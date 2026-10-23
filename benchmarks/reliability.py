"""Business-traffic HPA and two explicitly different recovery experiments."""
import datetime
import gzip
import json
import time

from audit import OUT, kubectl, now, save
from environment import NS, psql
from runner import case, configure, reset_data, state


def pods():
    return json.loads(kubectl('-n', NS, 'get', 'pods', '-l', 'app=core-api', '-o', 'json', explanation='Observe benchmark API identities and readiness').stdout)['items']


def fixed_two():
    kubectl('-n', NS, 'delete', 'hpa', 'bench-api', '--ignore-not-found=true', explanation='Disable only benchmark autoscaling for fixed-replica comparisons')
    kubectl('-n', NS, 'scale', 'deployment/bench-api', '--replicas=2', explanation='Restore two benchmark API replicas')
    kubectl('-n', NS, 'rollout', 'status', 'deployment/bench-api', '--timeout=300s', explanation='Wait for two ready benchmark replicas', timeout=360)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        current = pods()
        if len(current) == 2 and all(not p['metadata'].get('deletionTimestamp') and all(s['ready'] for s in p['status'].get('containerStatuses', [])) for p in current): return
        time.sleep(3)
    raise RuntimeError('Benchmark did not settle to exactly two ready API pods')


def first_request_time(folder):
    with gzip.open(folder / 'samples.json.gz', 'rt') as f:
        for line in f:
            row = json.loads(line)
            if row.get('type') == 'Point' and row.get('metric') == 'http_reqs': return row['data']['time']
    return None


def hpa(rate):
    fixed_two(); configure(True); reset_data()
    high = min(800, max(25, int(rate * 1.5)))
    control = case('hpa-fixed-two-control', high, seconds=240, mode='mixed', cache=True)
    print('HPA comparison cooldown: 120 seconds to avoid carrying old CPU load into scale-out timing', flush=True)
    time.sleep(120)
    original = {p['metadata']['uid'] for p in pods()}
    manifest = {'apiVersion': 'autoscaling/v2', 'kind': 'HorizontalPodAutoscaler', 'metadata': {'name': 'bench-api', 'namespace': NS},
                'spec': {'scaleTargetRef': {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'name': 'bench-api'},
                         'minReplicas': 2, 'maxReplicas': 5,
                         'metrics': [{'type': 'Resource', 'resource': {'name': 'cpu', 'target': {'type': 'Utilization', 'averageUtilization': 70}}}]}}
    save('benchmark-hpa.json', manifest)
    kubectl('apply', '-f', '-', explanation='Enable benchmark HPA with the same CPU target and bounds as production', stdin=json.dumps(manifest))
    reset_data()
    result = case('hpa-enabled', high, seconds=360, mode='mixed', cache=True, sample_interval=5)
    folder = OUT / 'cases/hpa-enabled'; observations = json.loads((folder / 'observations.json').read_text())
    first_extra_endpoint = None; first_scale = None; maximum_ready = 0
    for observation in observations:
        endpoints = [e for item in observation.get('endpoints', {}).get('items', []) for e in item.get('endpoints', []) if e.get('conditions', {}).get('ready')]
        ready = {e.get('targetRef', {}).get('uid') for e in endpoints}; maximum_ready = max(maximum_ready, len(ready))
        if len(ready) > 2 and ready - original and first_extra_endpoint is None: first_extra_endpoint = observation['utc']
        for item in observation.get('hpa', {}).get('items', []):
            if item.get('status', {}).get('desiredReplicas', 2) > 2 and first_scale is None:
                first_scale = {'observed_utc': observation['utc'], 'hpa_lastScaleTime': item['status'].get('lastScaleTime')}
    report = {'fixed_two': control, 'hpa_enabled': result, 'offered_rate': high, 'first_observed_request_end_utc': first_request_time(folder),
              'first_scale': first_scale, 'first_observed_extra_ready_endpoint_utc': first_extra_endpoint, 'maximum_ready_endpoints': maximum_ready,
              'limitations': ['Times include observation/request overhead; raw pod readiness and EndpointSlice states retained',
                             'If CPU does not cross the target, absence of scale-out is a result, not permission to substitute synthetic CPU burn',
                             'A database or shared-node bottleneck can limit the benefit of additional API replicas']}
    save('hpa-results.json', report)
    fixed_two()
    return report


def recovery(rate, abrupt=False):
    fixed_two(); configure(True); reset_data()
    name = 'recovery-container-crash' if abrupt else 'recovery-graceful-pod'
    load = max(5, int(rate * 0.4)); initial = pods(); target = sorted(initial, key=lambda p: p['metadata']['name'])[0]
    target_name = target['metadata']['name']; target_uid = target['metadata']['uid']; original_uids = {p['metadata']['uid'] for p in initial}
    assert target_name.startswith('bench-api-')
    initial_restarts = target['status']['containerStatuses'][0]['restartCount']

    def event(folder):
        details = {'mode': 'container SIGKILL' if abrupt else 'graceful pod deletion', 'target_pod': target_name, 'target_uid': target_uid,
                   'initial_restarts': initial_restarts, 'scheduled_delay_seconds_from_generator_launch': 45}
        try:
            time.sleep(45); details['action_start_utc'] = now()
            if abrupt:
                p = kubectl('-n', NS, 'exec', target_name, '--', '/usr/local/bin/python', '-c', 'import os,signal;os.kill(1,signal.SIGKILL)',
                            explanation='Crash only the selected benchmark API container process', required=False)
                details['action_return_code'] = p.returncode
            else:
                kubectl('-n', NS, 'delete', 'pod', target_name, '--wait=false', explanation='Gracefully delete exactly one benchmark API pod under business traffic')
            details['action_ack_utc'] = now(); deadline = time.monotonic() + 180; observations = []
            while time.monotonic() < deadline:
                current = pods()
                endpoint_data = json.loads(kubectl('-n', NS, 'get', 'endpointslices', '-l', 'kubernetes.io/service-name=bench-api', '-o', 'json', explanation='Observe ready service endpoints during recovery').stdout)
                ready_uids = {e.get('targetRef', {}).get('uid') for item in endpoint_data['items'] for e in item.get('endpoints', []) if e.get('conditions', {}).get('ready')}
                observations.append({'utc': now(), 'pods': [{'name': p['metadata']['name'], 'uid': p['metadata']['uid'], 'conditions': p['status'].get('conditions'), 'containers': p['status'].get('containerStatuses')} for p in current], 'ready_endpoint_uids': sorted(ready_uids)})
                recovered = [p for p in current if p['metadata']['uid'] in ready_uids and (
                    p['metadata']['uid'] == target_uid and p['status']['containerStatuses'][0]['restartCount'] > initial_restarts if abrupt else p['metadata']['uid'] not in original_uids)]
                if recovered and len(ready_uids) >= 2:
                    details.update(restored_utc=now(), recovered_pod=recovered[0]['metadata']['name'], recovered_uid=recovered[0]['metadata']['uid'], recovered=True)
                    break
                time.sleep(2)
            details['observations'] = observations
            details.setdefault('recovered', False)
        except Exception as error:
            details.update(error=str(error), recovered=False)
        save(str(folder.relative_to(OUT) / 'recovery-event.json'), details)

    result = case(name, load, seconds=180, mode='mixed', cache=True, event=event, sample_interval=5)
    event_data = json.loads((OUT / 'cases' / name / 'recovery-event.json').read_text())
    report = {'workload': result, 'event': event_data,
              'interpretation': 'Replica/container restoration time and HTTP impact; do not call zero HTTP failures a measured outage MTTR.'}
    successful_posts = 0; attempted_posts = 0
    with gzip.open(OUT / 'cases' / name / 'samples.json.gz', 'rt') as stream:
        for line in stream:
            row = json.loads(line)
            if row.get('type') == 'Point' and row.get('metric') == 'http_reqs' and row['data'].get('tags', {}).get('name') == 'create-item':
                attempted_posts += row['data']['value']
                if row['data']['tags'].get('status') == '201': successful_posts += row['data']['value']
    integrity = json.loads(psql("SELECT row_to_json(t) FROM (SELECT count(*) AS new_rows,count(DISTINCT content) AS unique_contents FROM items WHERE id>10000) t;").stdout)
    integrity.update(successful_post_responses=successful_posts, attempted_posts=attempted_posts,
                     all_acknowledged_posts_present=integrity['new_rows'] >= successful_posts,
                     no_duplicate_payload_rows=integrity['new_rows'] == integrity['unique_contents'],
                     note='Includes warmup and measured traffic. A failed POST response may still have committed; no exactly-once claim.')
    report['write_integrity'] = integrity
    if event_data.get('restored_utc'):
        report['action_to_observed_restoration_seconds'] = (datetime.datetime.fromisoformat(event_data['restored_utc']) - datetime.datetime.fromisoformat(event_data['action_start_utc'])).total_seconds()
    save(name + '-results.json', report)
    return report


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=['hpa', 'graceful', 'crash']); parser.add_argument('--rate', type=int, required=True)
    args = parser.parse_args()
    if args.action == 'hpa': hpa(args.rate)
    else: recovery(args.rate, abrupt=args.action == 'crash')
