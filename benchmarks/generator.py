"""Place the pinned generator in the existing free system-node pod slot."""
import json
import pathlib

from audit import kubectl, save
from environment import NS
from runner import K6

script = pathlib.Path(__file__).with_name('load.js').read_text()
config = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'benchmark-script', 'namespace': NS}, 'data': {'load.js': script}}
kubectl('apply', '-f', '-', explanation='Save exact load script in benchmark ConfigMap', stdin=json.dumps(config))
pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'bench-generator', 'namespace': NS, 'labels': {'app': 'load-generator', 'benchmark': 'threadops'}},
       'spec': {'restartPolicy': 'Never', 'activeDeadlineSeconds': 9900, 'automountServiceAccountToken': False,
                'nodeSelector': {'agentpool': 'systempool'},
                'securityContext': {'runAsUser': 12345, 'runAsGroup': 12345, 'runAsNonRoot': True, 'fsGroup': 12345},
                'containers': [{'name': 'generator', 'image': K6, 'command': ['/bin/sh', '-c', "trap 'exit 0' TERM INT; while :; do sleep 30; done"],
                                'env': [{'name': 'HOME', 'value': '/tmp'}],
                                'securityContext': {'readOnlyRootFilesystem': True, 'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                                'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '500m', 'memory': '512Mi'}},
                                'volumeMounts': [{'name': 'script', 'mountPath': '/scripts', 'readOnly': True}, {'name': 'results', 'mountPath': '/results'}, {'name': 'tmp', 'mountPath': '/tmp'}]}],
                'volumes': [{'name': 'script', 'configMap': {'name': 'benchmark-script'}}, {'name': 'results', 'emptyDir': {'sizeLimit': '1Gi'}}, {'name': 'tmp', 'emptyDir': {'sizeLimit': '64Mi'}}]}}
save('generator-pod.json', pod)
kubectl('apply', '-f', '-', explanation='Create generator on original node without increasing Azure capacity', stdin=json.dumps(pod))
kubectl('-n', NS, 'wait', '--for=condition=Ready', 'pod/bench-generator', '--timeout=180s', explanation='Wait for pinned generator readiness', timeout=240)
