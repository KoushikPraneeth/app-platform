"""Logged subprocess operations for the temporary AKS benchmark harness."""
import datetime
import hashlib
import json
import os
import pathlib
import shlex
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = pathlib.Path(os.environ.get('BENCH_EVIDENCE_DIR', ROOT / 'evidence/load-tests/20261023'))
OUT.mkdir(parents=True, exist_ok=True)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(name, obj):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + '\n')


def run(args, explanation, stdin=None, required=True, timeout=600, private=False):
    print(explanation, flush=True)
    start = now()
    p = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout)
    row = dict(start_utc=start, finish_utc=now(), command_arguments=args,
               command=shlex.join(args), explanation=explanation, exit_code=p.returncode,
               stdout=p.stdout if not private else '[Private resource fields omitted]',
               stderr=p.stderr)
    if stdin is not None:
        row['stdin_sha256'] = hashlib.sha256(stdin.encode()).hexdigest()
    with (OUT / 'commands-output.jsonl').open('a') as f:
        f.write(json.dumps(row) + '\n')
    if required and p.returncode:
        raise RuntimeError(explanation + ': ' + p.stderr[-2000:])
    return p


def kubectl(*args, explanation, **kwargs):
    return run(['kubectl', '--request-timeout=30s', *args], explanation, **kwargs)
