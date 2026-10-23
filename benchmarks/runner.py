"""A bounded k6 case with raw samples, cluster state and generator statistics."""
import argparse
import datetime
import gzip
import json
import os
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
    ]:
        p = kubectl(*args, explanation='Capture benchmark ' + name, required=False)
        if p.returncode == 0:
            data = json.loads(p.stdout)
            if name == 'deployments':
                data = [{'name': x['metadata']['name'], 'spec_replicas': x['spec'].get('replicas'), 'status': x.get('status')} for x in data['items']]
            if name == 'pods':
                data = [{'name': x['metadata']['name'], 'uid': x['metadata']['uid'], 'created': x['metadata']['creationTimestamp'],
                         'node': x['spec'].get('nodeName'), 'phase': x.get('status', {}).get('phase'),
                         'deleting': x['metadata'].get('deletionTimestamp'), 'conditions': x.get('status', {}).get('conditions'),
                         'container_statuses': x.get('status', {}).get('containerStatuses')} for x in data['items']]
            value[name] = data
    return dict(utc=now(), **value)


def case(name, rate, seconds=180, mode='read', cache=True, warmup=30, event=None, sample_interval=20):
    guard()
    folder = OUT / 'cases' / name
    assert not folder.exists(), 'Preserve previous case; use a unique name'
    folder.mkdir(parents=True)
    config = dict(name=name, offered_iterations_per_second=rate, measured_seconds=seconds, warmup_seconds=warmup,
                  mode=mode, cache_enabled=cache, thresholds={'p95_ms': 200, 'unexpected_error_rate': 0.01, 'dropped_iteration_rate': 0.01},
                  generator='k6 Docker container on user Mac, external HTTP through existing AKS ingress', image=K6,
                  target='http://' + HOST, resources={'generator_cpus': 2, 'generator_memory': '1g'}, started_utc=now())
    save(str(folder.relative_to(OUT) / 'configuration.json'), config)
    container = 'threadops-k6-' + name.replace('_', '-')[:48]
    args = ['docker', 'run', '--rm', '--name', container, '--cpus', '2', '--memory', '1g',
            '--user', str(os.getuid()) + ':' + str(os.getgid()), '-e', 'HOME=/tmp',
            '--add-host', HOST + ':' + INGRESS_IP, '-v', str(pathlib.Path(__file__).parent.resolve()) + ':/scripts:ro',
            '-v', str(folder.resolve()) + ':/results', '-e', 'BASE_URL=http://' + HOST, '-e', 'RATE=' + str(rate),
            '-e', 'SECONDS=' + str(seconds), '-e', 'WARMUP=' + str(warmup), '-e', 'MODE=' + mode,
            '-e', 'CACHE_EXPECT=' + ('enabled' if cache else 'bypass'), '-e', 'RUN_ID=' + name,
            K6, 'run', '--out', 'json=/results/samples.json.gz', '/scripts/load.js']
    stop = threading.Event(); observations = []; generator = []
    pf_log = (folder / 'prometheus-port-forward.log').open('w')
    pf_args = ['kubectl', '-n', 'monitoring', 'port-forward', 'pod/prometheus-prometheus-kube-prometheus-prometheus-0', '19090:9090']
    pf = subprocess.Popen(pf_args, stdout=pf_log, stderr=subprocess.STDOUT, text=True)
    queries = {
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
                p = run(['docker', 'stats', '--no-stream', '--format', '{{json .}}', container], 'Capture load-generator resource use', required=False, timeout=30)
                if p.returncode == 0 and p.stdout.strip(): generator.append(dict(utc=now(), stats=json.loads(p.stdout)))
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
        run(['docker', 'stop', '--time', '5', container], 'Stop bounded benchmark after its timeout', required=False)
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
    if not (folder / 'summary.json').exists(): raise RuntimeError('k6 did not produce a summary: ' + str(folder))
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
    result['successful_requests_per_second'] = result['successful_requests'] / seconds
    save(str(folder.relative_to(OUT) / 'result.json'), result)
    print(json.dumps(result, indent=2), flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('name'); parser.add_argument('--rate', type=int, required=True)
    parser.add_argument('--seconds', type=int, default=180); parser.add_argument('--mode', choices=['read', 'mixed'], default='read')
    parser.add_argument('--cache', choices=['true', 'false'], default='true'); parser.add_argument('--warmup', type=int, default=30)
    args = parser.parse_args(); configure(args.cache == 'true'); reset_data()
    case(args.name, args.rate, args.seconds, args.mode, args.cache == 'true', args.warmup)
