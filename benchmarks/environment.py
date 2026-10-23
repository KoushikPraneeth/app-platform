"""Temporary isolated workloads. Provisioning must follow capacity-approval.json."""
import argparse
import datetime
import json
import pathlib
import subprocess
import sys
import urllib.parse

from audit import OUT, ROOT, kubectl, now, run, save

NS = 'threadops-benchmark'
POOL = 'bench23'
DB = 'threadops_bench_20261023'
HOST = 'benchmark.threadops.invalid'
ORG = ['--resource-group', 'rgdev', '--cluster-name', 'aksdev']


def psql(sql, database=DB):
    return kubectl('exec', '-i', '-n', 'default', 'postgres-0', '--', 'psql', '-v', 'ON_ERROR_STOP=1',
                   '-U', 'postgres', '-d', database, '-At', explanation='Execute saved SQL in isolated benchmark database', stdin=sql)


def provision():
    approval = json.loads((OUT / 'capacity-approval.json').read_text())
    assert approval['temporary_pool'] == POOL and approval['node_count'] == 1
    assert not (OUT / 'capacity-session.json').exists(), 'A capacity session already exists; inspect before resuming'
    pools = json.loads(run(['az', 'aks', 'nodepool', 'list', *ORG, '-o', 'json'], 'Record node pools before temporary capacity').stdout)
    assert len(pools) == 1 and pools[0]['name'] == 'systempool' and pools[0]['count'] == 1
    state = {'created_utc': now(), 'pool': POOL, 'maximum_session_seconds': 10800,
             'cleanup_start_deadline_utc': (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=9900)).isoformat(),
             'status': 'provisioning'}
    save('capacity-session.json', state)
    guard_log = (OUT / 'capacity-guardian.log').open('a')
    guardian = subprocess.Popen([sys.executable, str(pathlib.Path(__file__).with_name('guardian.py'))], stdout=guard_log, stderr=subprocess.STDOUT, start_new_session=True)
    guard_log.close()
    state['guardian_pid'] = guardian.pid; save('capacity-session.json', state)
    args = ['az', 'aks', 'nodepool', 'add', *ORG, '--name', POOL, '--mode', 'User', '--node-count', '1',
            '--node-vm-size', 'Standard_DS2_v2', '--max-pods', '30', '--node-osdisk-size', '128',
            '--vnet-subnet-id', pools[0]['vnetSubnetId'],
            '--node-taints', 'benchmark=threadops:NoSchedule', '--labels', 'benchmark=threadops', '-o', 'json']
    run(args, 'Provision the explicitly approved, tainted temporary benchmark node', timeout=1200)
    state.update(status='ready', ready_utc=now()); save('capacity-session.json', state)
    kubectl('wait', '--for=condition=Ready', 'nodes', '-l', 'agentpool=' + POOL, '--timeout=600s',
            explanation='Wait for the dedicated benchmark node', timeout=660)


def apply(image):
    assert (OUT / 'capacity-session.json').exists()
    kubectl('create', 'namespace', NS, explanation='Create isolated temporary benchmark namespace')
    psql('CREATE DATABASE ' + DB + ';', database='postgres')
    psql((pathlib.Path(__file__).parent / 'seed.sql').read_text())
    # Read only DATABASE_URL from the existing deployment in memory; never persist the credential.
    payload = json.loads(kubectl('get', 'deployment', 'core-api', '-n', 'default', '-o', 'json',
                                explanation='Read database connection for isolated database credential reuse', private=True).stdout)
    url = next(e['value'] for e in payload['spec']['template']['spec']['containers'][0]['env'] if e['name'] == 'DATABASE_URL')
    parsed = urllib.parse.urlsplit(url)
    url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc.replace('@postgres:', '@postgres.default.svc.cluster.local:'), '/' + DB, '', ''))
    secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'benchmark-database', 'namespace': NS},
              'stringData': {'DATABASE_URL': url}}
    kubectl('apply', '-f', '-', explanation='Create benchmark database Secret without recording credentials', stdin=json.dumps(secret), private=True)
    notify = next(c['image'] for c in json.loads(kubectl('get', 'deployment', 'notify-service', '-n', 'default', '-o', 'json',
                  explanation='Record unchanged audit runtime', private=True).stdout)['spec']['template']['spec']['containers'])
    objects = []
    for name, app, ref, port, replicas in [('bench-api', 'core-api', image, 8000, 2), ('bench-audit', 'notify-service', notify, 8001, 1)]:
        env = []
        if app == 'core-api':
            env = [{'name': 'DATABASE_URL', 'valueFrom': {'secretKeyRef': {'name': 'benchmark-database', 'key': 'DATABASE_URL'}}}]
            env += [{'name': k, 'value': v} for k, v in {
                'REDIS_URL': 'redis://redis.default.svc.cluster.local:6379/0', 'REDIS_KEY_PREFIX': 'bench:20261023:',
                'CACHE_ENABLED': 'true', 'RATE_LIMIT_MAX_REQUESTS': '1000000', 'DB_POOL_SIZE': '5', 'DB_MAX_OVERFLOW': '5',
                'NOTIFY_SERVICE_URL': 'http://bench-audit:8001',
            }.items()]
        labels = {'app': app, 'benchmark': 'threadops'}
        container = {'name': app, 'image': ref, 'ports': [{'containerPort': port}], 'env': env,
                     'securityContext': {'runAsUser': 10001, 'runAsGroup': 10001, 'runAsNonRoot': True, 'allowPrivilegeEscalation': False},
                     'readinessProbe': {'httpGet': {'path': '/readyz' if app == 'core-api' else '/healthz', 'port': port},
                                        'initialDelaySeconds': 15 if app == 'core-api' else 10, 'periodSeconds': 10},
                     'livenessProbe': {'httpGet': {'path': '/healthz', 'port': port}, 'initialDelaySeconds': 15, 'periodSeconds': 10},
                     'resources': {'requests': {'cpu': '100m' if app == 'core-api' else '50m', 'memory': '128Mi' if app == 'core-api' else '64Mi'},
                                   'limits': {'cpu': '300m' if app == 'core-api' else '200m', 'memory': '256Mi' if app == 'core-api' else '128Mi'}}}
        objects += [{'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': name, 'namespace': NS},
                     'spec': {'replicas': replicas, 'selector': {'matchLabels': labels}, 'strategy': {'type': 'RollingUpdate', 'rollingUpdate': {'maxSurge': 1, 'maxUnavailable': 0}},
                              'template': {'metadata': {'labels': labels}, 'spec': {'nodeSelector': {'agentpool': POOL},
                               'tolerations': [{'key': 'benchmark', 'operator': 'Equal', 'value': 'threadops', 'effect': 'NoSchedule'}],
                               'automountServiceAccountToken': False, 'containers': [container]}}}},
                    {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': name, 'namespace': NS, 'labels': labels},
                     'spec': {'selector': labels, 'ports': [{'name': 'http', 'port': port, 'targetPort': port}]}}]
    objects.append({'apiVersion': 'monitoring.coreos.com/v1', 'kind': 'ServiceMonitor',
                    'metadata': {'name': 'benchmark-api', 'namespace': NS, 'labels': {'release': 'prometheus'}},
                    'spec': {'selector': {'matchLabels': {'app': 'core-api'}},
                             'endpoints': [{'port': 'http', 'path': '/metrics', 'interval': '15s'}]}})
    objects.append({'apiVersion': 'networking.k8s.io/v1', 'kind': 'Ingress', 'metadata': {'name': 'benchmark', 'namespace': NS},
                    'spec': {'ingressClassName': 'nginx', 'rules': [{'host': HOST, 'http': {'paths': [{'path': '/api', 'pathType': 'Prefix',
                     'backend': {'service': {'name': 'bench-api', 'port': {'number': 8000}}}}]}}]}})
    objects.append({'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': {'name': 'audit-isolation', 'namespace': NS},
                    'spec': {'podSelector': {'matchLabels': {'app': 'notify-service'}}, 'policyTypes': ['Ingress'], 'ingress': [
                     {'from': [{'podSelector': {'matchLabels': {'app': 'core-api'}}}], 'ports': [{'protocol': 'TCP', 'port': 8001}]}]}})
    manifest = {'apiVersion': 'v1', 'kind': 'List', 'items': objects}; save('benchmark-resources.json', manifest)
    kubectl('apply', '-f', '-', explanation='Deploy isolated benchmark workloads without changing production configuration', stdin=json.dumps(manifest))
    for name in ['bench-api', 'bench-audit']:
        kubectl('-n', NS, 'rollout', 'status', 'deployment/' + name, '--timeout=300s', explanation='Verify benchmark workload readiness: ' + name, timeout=360)
    save('environment.json', {'namespace': NS, 'pool': POOL, 'database': DB, 'host': HOST, 'image': image, 'created_utc': now()})


def cleanup():
    state = json.loads((OUT / 'capacity-session.json').read_text()) if (OUT / 'capacity-session.json').exists() else {}
    kubectl('delete', 'namespace', NS, '--ignore-not-found=true', '--wait=true', '--timeout=120s',
            explanation='Remove only temporary benchmark namespace and workloads', required=False, timeout=180)
    errors = []
    try:
        psql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='" + DB + "' AND pid<>pg_backend_pid();\nDROP DATABASE IF EXISTS " + DB + ';', database='postgres')
        kubectl('exec', '-i', '-n', 'default', 'deploy/core-api', '--', '/usr/local/bin/python', '-',
                explanation='Remove only the benchmark Redis key prefix', stdin="import redis,os\nr=redis.from_url(os.environ['REDIS_URL'])\nkeys=list(r.scan_iter(match='bench:20261023:*'))\nprint('benchmark_keys_removed',r.delete(*keys) if keys else 0)\n", required=False)
    except Exception as error:
        errors.append(str(error))
    if state:
        current = json.loads(run(['az', 'aks', 'nodepool', 'list', *ORG, '-o', 'json'], 'Check whether temporary capacity still exists before deletion').stdout)
        if any(p['name'] == POOL for p in current):
            run(['az', 'aks', 'nodepool', 'delete', *ORG, '--name', POOL, '--yes', '-o', 'json'],
                'Delete the approved temporary benchmark node pool', timeout=1200)
        remaining = json.loads(run(['az', 'aks', 'nodepool', 'list', *ORG, '-o', 'json'], 'Verify temporary pool removal and original pool count').stdout)
        assert all(p['name'] != POOL for p in remaining)
        assert next(p for p in remaining if p['name'] == 'systempool')['count'] == 1
        state.update(status='removed', removed_utc=now(), cleanup_errors=errors, remaining_pools=[{'name': p['name'], 'count': p['count']} for p in remaining]); save('capacity-session.json', state)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=['provision', 'apply', 'cleanup']); parser.add_argument('--image')
    args = parser.parse_args()
    if args.action == 'provision': provision()
    elif args.action == 'apply':
        assert args.image
        apply(args.image)
    else: cleanup()
