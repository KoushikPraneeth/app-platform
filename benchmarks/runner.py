"""A bounded k6 case with raw samples, cluster state and generator statistics."""
import argparse
import datetime
import contextlib
import gzip
import hashlib
import json
import os
import re
import pathlib
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from audit import OUT, ROOT, kubectl, now, run, save
from environment import DB, HOST, NS, psql

K6 = 'grafana/k6@sha256:9c2dee7f8ed74d317e4027c06a10f169b625638189de8d4555d0b3486a5aeb34'
INGRESS_IP = '9.160.184.66'


def guard():
    state = json.loads((OUT / 'capacity-session.json').read_text())
    assert state['status'] == 'ready'
    assert datetime.datetime.now(datetime.timezone.utc) < datetime.datetime.fromisoformat(state['cleanup_start_deadline_utc']), 'Capacity session deadline reached; cleanup required'


def configure(cache=True, image=None):
    guard()
    kubectl('-n', NS, 'set', 'env', 'deployment/bench-api', 'CACHE_ENABLED=' + str(cache).lower(), explanation='Set explicit benchmark cache mode with Redis remaining available')
    if image:
        kubectl('-n', NS, 'set', 'image', 'deployment/bench-api', 'core-api=' + image, explanation='Set benchmark application image')
    kubectl('-n', NS, 'rollout', 'status', 'deployment/bench-api', '--timeout=300s', explanation='Wait for benchmark configuration rollout', timeout=360)
    current = kubectl('-n', NS, 'get', 'deployment', 'bench-api', '-o', 'jsonpath={.spec.template.spec.containers[0].image}', explanation='Record exact benchmark application image').stdout.strip()
    save('current-configuration.json', {'cache_enabled': cache, 'application_image': current, 'utc': now()})


def reset_data():
    psql("TRUNCATE items RESTART IDENTITY; INSERT INTO items SELECT * FROM fixture_items; SELECT setval(pg_get_serial_sequence('items','id'),10000,true); ANALYZE items;")
    kubectl('exec', '-i', '-n', 'default', 'deploy/core-api', '--', '/usr/local/bin/python', '-',
            explanation='Clear only benchmark cache and rate counters between cases, outside timed load',
            stdin="import os,redis\nr=redis.from_url(os.environ['REDIS_URL'])\nkeys=list(r.scan_iter(match='bench:20261023:*'))\nprint('benchmark_keys_removed',r.delete(*keys) if keys else 0)\n")


def state():
    value = {}
    for name, args in [
        ('pods', ['-n', NS, 'get', 'pods', '-o', 'json']),
        ('deployments', ['-n', NS, 'get', 'deployments', '-o', 'json']),
        ('hpa', ['-n', NS, 'get', 'hpa', '-o', 'json']),
        ('endpoints', ['-n', NS, 'get', 'endpointslices', '-l', 'kubernetes.io/service-name=bench-api', '-o', 'json']),
        ('working_set_and_cpu', ['get', '--raw', '/apis/metrics.k8s.io/v1beta1/namespaces/' + NS + '/pods']),
        ('node_working_set_and_cpu', ['get', '--raw', '/apis/metrics.k8s.io/v1beta1/nodes']),
    ]:
        p = kubectl(*args, explanation='Capture benchmark ' + name, required=False)
        if p.returncode == 0:
            data = json.loads(p.stdout)
            if name == 'deployments':
                data = [{'name': x['metadata']['name'], 'spec_replicas': x['spec'].get('replicas'), 'status': x.get('status')} for x in data['items']]
            if name == 'pods':
                data = [{'name': x['metadata']['name'], 'uid': x['metadata']['uid'], 'created': x['metadata']['creationTimestamp'],
                         'node': x['spec'].get('nodeName'), 'app': x['metadata'].get('labels', {}).get('app'), 'phase': x.get('status', {}).get('phase'),
                         'deleting': x['metadata'].get('deletionTimestamp'), 'conditions': x.get('status', {}).get('conditions'),
                         'container_statuses': x.get('status', {}).get('containerStatuses')} for x in data['items']]
            value[name] = data
    return dict(utc=now(), **value)


def case(name, rate, seconds=180, mode='read', cache=True, warmup=30, event=None, sample_interval=20):
    guard()
    assert re.fullmatch(r'[a-z0-9-]+', name), 'Invalid case name'
    session = json.loads((OUT / 'capacity-session.json').read_text())
    remaining = (datetime.datetime.fromisoformat(session['cleanup_start_deadline_utc']) - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    assert remaining > seconds + warmup + 300, 'Insufficient approved session time; cleanup required'
    previous_wire_bytes = sum(json.loads(p.read_text()).get('client_wire_bytes', 0) for p in (OUT / 'cases').glob('*/result.json')) if (OUT / 'cases').exists() else 0
    assert previous_wire_bytes + rate * (seconds + warmup) * 18000 < 50000000000, 'Bounded 50 GB in-cluster traffic allowance reached; stop and review before more load'
    folder = OUT / 'cases' / name
    if (folder / 'result.json').exists():
        existing = json.loads((folder / 'result.json').read_text())
        cfg = existing['configuration']
        assert (cfg['offered_iterations_per_second'], cfg['measured_seconds'], cfg['mode'], cfg['cache_enabled']) == (rate, seconds, mode, cache), 'Saved case configuration mismatch'
        assert cfg['load_script_sha256'] == hashlib.sha256(pathlib.Path(__file__).with_name('load.js').read_bytes()).hexdigest(), 'Saved load script mismatch'
        print('Reuse complete saved case without rerunning traffic: ' + name, flush=True)
        return existing
    assert not folder.exists(), 'Incomplete case must be recovered or archived before retrying'
    folder.mkdir(parents=True)
    config = dict(name=name, offered_iterations_per_second=rate, measured_seconds=seconds, warmup_seconds=warmup,
                  mode=mode, cache_enabled=cache, thresholds={'p95_ms': 200, 'unexpected_error_rate': 0.01, 'dropped_iteration_rate': 0.01},
                  generator='k6 pod on original system node; NGINX ClusterIP ingress to API on dedicated benchmark node; excludes public internet and Azure public load balancer', image=K6,
                  target='http://ingress-nginx-controller.ingress-nginx.svc.cluster.local with Host ' + HOST, resources={'generator_cpu_request': '100m', 'generator_cpu_limit': '500m', 'generator_memory_limit': '512Mi'}, started_utc=now())
    config['load_script_sha256'] = hashlib.sha256(pathlib.Path(__file__).with_name('load.js').read_bytes()).hexdigest()
    config['application_configuration'] = json.loads((OUT / 'current-configuration.json').read_text())
    save(str(folder.relative_to(OUT) / 'configuration.json'), config)
    kubectl('-n', NS, 'exec', 'bench-generator', '--', 'mkdir', '-p', '/results/' + name,
            explanation='Create isolated generator output directory')
    kubectl('-n', NS, 'exec', 'bench-generator', '--', 'rm', '-f', '/results/summary.json',
            explanation='Remove only the previous generator summary to prevent stale-result attribution')
    remote_folder = '/results/' + name
    args = ['kubectl', '--request-timeout=0', '-n', NS, 'exec', 'bench-generator', '--', 'env',
            'BASE_URL=http://ingress-nginx-controller.ingress-nginx.svc.cluster.local', 'HOST=' + HOST,
            'RATE=' + str(rate), 'SECONDS=' + str(seconds), 'WARMUP=' + str(warmup), 'MODE=' + mode,
            'CACHE_EXPECT=' + ('enabled' if cache else 'bypass'), 'RUN_ID=' + name,
            '/usr/bin/k6', 'run', '--out', 'json=' + remote_folder + '/samples.json.gz', '/scripts/load.js']
    stop = threading.Event(); observations = []; generator = []
    pf_log = (folder / 'prometheus-port-forward.log').open('w')
    pf_args = ['kubectl', '-n', 'monitoring', 'port-forward', 'pod/prometheus-prometheus-kube-prometheus-prometheus-0', '19090:9090']
    pf = subprocess.Popen(pf_args, stdout=pf_log, stderr=subprocess.STDOUT, text=True)
    queries = {
        'generator_cpu_throttled_period_fraction': 'sum(rate(container_cpu_cfs_throttled_periods_total{namespace=\"' + NS + '\",container=\"generator\"}[1m])) / sum(rate(container_cpu_cfs_periods_total{namespace=\"' + NS + '\",container=\"generator\"}[1m]))',
        'api_cpu_cores': 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="' + NS + '",container="core-api"}[1m]))',
        'api_cpu_throttled_period_fraction': 'sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{namespace="' + NS + '",container="core-api"}[1m])) / sum by (pod) (rate(container_cpu_cfs_periods_total{namespace="' + NS + '",container="core-api"}[1m]))',
        'api_working_set_bytes': 'sum by (pod) (container_memory_working_set_bytes{namespace="' + NS + '",container="core-api"})',
        'business_handler_requests_per_second': 'sum(rate(http_requests_total{namespace="' + NS + '",handler="/api/items"}[1m]))',
    }

    def observe():
        while not stop.is_set():
            try:
                observation = state(); observation['prometheus'] = {}
                for key, query in queries.items():
                    try:
                        url = 'http://127.0.0.1:19090/api/v1/query?' + urllib.parse.urlencode({'query': query})
                        with urllib.request.urlopen(url, timeout=3) as response:
                            observation['prometheus'][key] = {'query': query, 'response': json.load(response)}
                    except Exception as error:
                        observation['prometheus'][key] = {'query': query, 'error': str(error)}
                observations.append(observation)
                for pod in observation.get('working_set_and_cpu', {}).get('items', []):
                    if pod['metadata']['name'] != 'bench-generator': continue
                    usage = pod['containers'][0]['usage']
                    cpu_value = usage['cpu']
                    cpu_cores = float(cpu_value[:-1])/1000000000 if cpu_value.endswith('n') else float(cpu_value[:-1])/1000 if cpu_value.endswith('m') else float(cpu_value)
                    memory_value = usage['memory']
                    memory_bytes = float(memory_value[:-2])*1024 if memory_value.endswith('Ki') else float(memory_value[:-2])*1048576 if memory_value.endswith('Mi') else float(memory_value)
                    generator.append(dict(utc=now(), metrics_timestamp=pod['timestamp'], cpu_cores=cpu_cores, memory_bytes=memory_bytes))
                save(str(folder.relative_to(OUT) / 'observations.json'), observations)
                save(str(folder.relative_to(OUT) / 'generator-stats.json'), generator)
            except Exception as error:
                with (folder / 'observation-errors.txt').open('a') as f: f.write(str(error) + '\n')
            stop.wait(sample_interval)

    watcher = threading.Thread(target=observe, daemon=True)
    output = (folder / 'console-output.txt').open('w')
    started = now(); process = subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT, text=True)
    watcher.start()
    event_thread = None
    if event:
        event_thread = threading.Thread(target=event, args=(folder,), daemon=True); event_thread.start()
    try:
        code = process.wait(timeout=seconds + warmup + 120)
    except subprocess.TimeoutExpired:
        kubectl('-n', NS, 'exec', 'bench-generator', '--', '/bin/sh', '-c', 'kill -INT $(pidof k6)', explanation='Stop bounded generator after timeout', required=False)
        code = process.wait(timeout=30)
    finally:
        output.close(); stop.set(); watcher.join(timeout=180)
        if event_thread: event_thread.join(timeout=180)
        pf.terminate()
        try: pf.wait(timeout=10)
        except subprocess.TimeoutExpired: pf.kill(); pf.wait()
        pf_log.close()
        with (OUT / 'commands-output.jsonl').open('a') as f:
            f.write(json.dumps(dict(command_arguments=pf_args, explanation='Temporary local Prometheus port-forward for benchmark telemetry; terminated after case', output_file=str((folder / 'prometheus-port-forward.log').relative_to(OUT)))) + '\n')
    with (OUT / 'commands-output.jsonl').open('a') as f:
        f.write(json.dumps(dict(command_arguments=args, explanation='Run fixed-arrival-rate AKS application workload; raw samples compressed without discarding failures',
                                started_utc=started, finished_utc=now(), exit_code=code, output_file=str((folder / 'console-output.txt').relative_to(OUT)))) + '\n')
    for filename in ['summary.json', 'samples.json.gz']:
        remote = '/results/summary.json' if filename == 'summary.json' else remote_folder + '/' + filename
        download_artifact(remote, folder / filename)
    kubectl('-n', NS, 'exec', 'bench-generator', '--', 'rm', '-rf', remote_folder, explanation='Remove only downloaded case files from temporary generator storage')
    if not (folder / 'summary.json').exists(): raise RuntimeError('k6 did not produce a summary: ' + str(folder))
    return summarize(folder, config, code, generator)



def download_artifact(remote, target):
    # Copy via the older streaming transport after repeated websocket/tar EOFs.
    # Verify exact remote/local SHA-256; a transfer retry never reruns workload.
    env = dict(os.environ, KUBECTL_REMOTE_COMMAND_WEBSOCKETS='false')
    command = ['kubectl', '--request-timeout=0', '-n', NS, 'exec', 'bench-generator', '--', 'cat', remote]
    expected = kubectl('-n', NS, 'exec', 'bench-generator', '--', 'sha256sum', remote,
                       explanation='Fingerprint completed remote benchmark artifact').stdout.split()[0]
    for attempt in range(1, 4):
        started = now()
        with target.open('wb') as stream:
            p = subprocess.run(command, env=env, stdout=stream, stderr=subprocess.PIPE, timeout=240)
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        row = dict(command_arguments=command, explanation='Retrieve exact completed artifact with streaming transport and SHA-256 verification',
                   environment_overrides={'KUBECTL_REMOTE_COMMAND_WEBSOCKETS': 'false'}, started_utc=started, finished_utc=now(),
                   exit_code=p.returncode, stderr=p.stderr.decode(errors='replace'), attempt=attempt,
                   expected_sha256=expected, actual_sha256=actual, output_file=str(target.relative_to(OUT)), bytes=target.stat().st_size)
        with (OUT / 'commands-output.jsonl').open('a') as log: log.write(json.dumps(row) + '\n')
        if p.returncode == 0 and actual == expected: return
    raise RuntimeError('Artifact failed exact-content verification: ' + str(target))


def summarize(folder, config, code, generator):
    summary = json.loads((folder / 'summary.json').read_text()); metrics = summary['metrics']
    def values(key): return metrics.get(key, {}).get('values', {})
    passed = all(t['ok'] for metric in metrics.values() for t in metric.get('thresholds', {}).values())
    statuses = {}; first = None; last = None
    with gzip.open(folder / 'samples.json.gz', 'rt') as f:
        for line in f:
            row = json.loads(line)
            if row.get('type') != 'Point' or row.get('metric') != 'http_reqs': continue
            data = row['data']
            if data.get('tags', {}).get('scenario') != 'measured': continue
            status = data.get('tags', {}).get('status', 'unknown'); statuses[status] = statuses.get(status, 0) + data['value']
            first = first or data['time']; last = data['time']
    result = dict(configuration=config, exit_code=code, thresholds_passed=passed,
                  successful_requests=values('successful_requests{scenario:measured}').get('count', 0),
                  success_duration=values('successful_duration{scenario:measured}'),
                  business_failures=values('business_failures{scenario:measured}'),
                  dropped_iterations=values('dropped_iterations{scenario:measured}').get('count', 0),
                  cache_hit_fraction=values('cache_hit_fraction{scenario:measured}').get('rate'),
                  status_counts=statuses, first_measured_request_end=first, last_measured_request_end=last)
    result['successful_requests_per_second'] = result['successful_requests'] / config['measured_seconds']
    result['client_wire_bytes'] = values('data_received').get('count', 0) + values('data_sent').get('count', 0)
    cpu = [r['cpu_cores'] / 0.5 * 100 for r in generator]
    memory = [r['memory_bytes'] / (512 * 1048576) * 100 for r in generator]
    result['generator_near_resource_limit'] = sum(x >= 90 for x in cpu) >= 2 or sum(x >= 90 for x in memory) >= 2
    result['generator_max_cpu_limit_percent'] = max(cpu) if cpu else None
    result['generator_max_memory_percent'] = max(memory) if memory else None
    result['measurement_valid'] = bool(generator) and not result['generator_near_resource_limit']
    save(str(folder.relative_to(OUT) / 'result.json'), result)
    print(json.dumps(result, indent=2), flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('name'); parser.add_argument('--rate', type=int, required=True)
    parser.add_argument('--seconds', type=int, default=180); parser.add_argument('--mode', choices=['read', 'mixed'], default='read')
    parser.add_argument('--cache', choices=['true', 'false'], default='true'); parser.add_argument('--warmup', type=int, default=30)
    args = parser.parse_args(); configure(args.cache == 'true'); reset_data()
    case(args.name, args.rate, args.seconds, args.mode, args.cache == 'true', args.warmup)
