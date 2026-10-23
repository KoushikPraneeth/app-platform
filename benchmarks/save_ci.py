"""Save CI source identity, full logs and scan artifacts without persisting signed URLs."""
import argparse
import io
import json
import pathlib
import urllib.request
import zipfile

from audit import OUT, run, save


def collect(label, sha, azure_id):
    folder = OUT / 'ci' / label; folder.mkdir(parents=True, exist_ok=True)
    gh = json.loads(run(['gh', 'run', 'list', '--repo', 'KoushikPraneeth/app-platform', '--commit', sha,
                         '--json', 'databaseId,name,status,conclusion,url'], 'Read source-matched GitHub CI runs').stdout)
    save(str(folder.relative_to(OUT) / 'github-runs.json'), gh)
    assert gh and all(r['status'] == 'completed' and r['conclusion'] == 'success' for r in gh)
    for r in gh:
        sub = folder / ('github-' + str(r['databaseId'])); sub.mkdir(exist_ok=True)
        p = run(['gh', 'run', 'view', str(r['databaseId']), '--repo', 'KoushikPraneeth/app-platform', '--log'], 'Save complete successful GitHub workflow log')
        (sub / 'complete.log').write_text(p.stdout)
        run(['gh', 'run', 'download', str(r['databaseId']), '--repo', 'KoushikPraneeth/app-platform', '--pattern', 'trivy-*', '--dir', str(sub)], 'Download GitHub runtime scan artifacts')
    base = ['--organization', 'https://dev.azure.com/pkoushik-platform']
    p = run(['az', 'pipelines', 'runs', 'show', '--id', str(azure_id), *base, '--project', 'CloudNative-GitOps-Platform', '-o', 'json'], 'Verify Azure CI source and successful result')
    az = json.loads(p.stdout)
    assert az['sourceVersion'] == sha and az['status'] == 'completed' and az['result'] == 'succeeded'
    save(str(folder.relative_to(OUT) / 'azure-run.json'), {k: az.get(k) for k in ['id','buildNumber','sourceVersion','status','result','reason','queueTime','startTime','finishTime','_links']})
    log = folder / 'azure-logs.zip'
    run(['az', 'devops', 'invoke', *base, '--area', 'build', '--resource', 'logs', '--route-parameters', 'project=CloudNative-GitOps-Platform', 'buildId=' + str(azure_id), '--accept-media-type', 'application/zip', '--out-file', str(log), '-o', 'json'], 'Download complete Azure pipeline log ZIP')
    with zipfile.ZipFile(log) as z:
        for n in z.namelist():
            path = pathlib.Path(n)
            if n.endswith('/') or path.is_absolute() or '..' in path.parts or path.suffix.lower() in ['.md', '.markdown']: continue
            target = folder / 'azure-logs' / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(z.read(n))
    p = run(['az', 'devops', 'invoke', *base, '--area', 'pipelines', '--resource', 'artifacts', '--route-parameters',
             'project=CloudNative-GitOps-Platform', 'pipelineId=1', 'runId=' + str(azure_id), '--query-parameters',
             'artifactName=runtime-security-reports', '$expand=signedContent', '--api-version', '7.1', '-o', 'json'],
            'Request temporary signed link for Azure runtime scan artifact', private=True)
    with urllib.request.urlopen(json.loads(p.stdout)['signedContent']['url'], timeout=60) as response: data = response.read()
    (folder / 'azure-security-reports.zip').write_bytes(data)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            path = pathlib.Path(n)
            if n.endswith('/') or path.is_absolute() or '..' in path.parts or path.suffix != '.json': continue
            target = folder / 'azure-security' / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(z.read(n))
    print('Saved source-matched successful CI logs and runtime scan artifacts: ' + label, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('label'); parser.add_argument('sha'); parser.add_argument('azure_id', type=int)
    args = parser.parse_args(); collect(args.label, args.sha, args.azure_id)
