#!/usr/bin/env python3
"""Build the outbound Agent client only; never package the orchestration server."""
import argparse
import ast
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import zipfile

FILES = ('local_connector.py', 'codex_agent_adapter.py')
DENIED = ('cloud_platform', 'cloud_common', 'agents_service', 'strategy_studio_service', 'ati_shared_sdk', 'ati_cloud_llm')


def build(source, output):
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain'], text=True).strip():
        raise ValueError('Agent client source must be committed and clean')
    version = '0.1.0+' + revision[:12]
    dist = 'ati_local_agent_client-' + version + '.dist-info'
    files = {'worker_orchestrator/__init__.py': b'"""ATI outbound local Agent client; no server entrypoint."""\n'}
    inventory = []
    for name in FILES:
        path = source / 'src/worker_orchestrator' / name
        if path.is_symlink():
            raise ValueError('Client source symlink rejected')
        content = path.read_bytes()
        for node in ast.walk(ast.parse(content)):
            imports = [x.name for x in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) else []
            if any(value == prefix or value.startswith(prefix + '.') for value in imports for prefix in DENIED):
                raise ValueError('Private platform dependency in Agent client')
        files['worker_orchestrator/' + name] = content
        inventory.append({'path': 'src/worker_orchestrator/' + name, 'sha256': hashlib.sha256(content).hexdigest()})
    files[dist + '/METADATA'] = ('Metadata-Version: 2.1\nName: ati-local-agent-client\nVersion: ' + version + '\nSummary: ATI outbound local Codex Agent client\nRequires-Python: >=3.11\nRequires-Dist: httpx>=0.27,<1\n\n').encode()
    files[dist + '/WHEEL'] = b'Wheel-Version: 1.0\nGenerator: ati-agent-client\nRoot-Is-Purelib: true\nTag: py3-none-any\n'
    record = io.StringIO(newline='')
    writer = csv.writer(record, lineterminator='\n')
    for name, content in sorted(files.items()):
        writer.writerow([name, 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip('='), len(content)])
    writer.writerow([dist + '/RECORD', '', ''])
    files[dist + '/RECORD'] = record.getvalue().encode()
    output.mkdir(parents=True, exist_ok=True)
    wheel = output / ('ati_local_agent_client-' + version + '-py3-none-any.whl')
    with zipfile.ZipFile(wheel, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            item = zipfile.ZipInfo(name, (2024, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o644 << 16
            archive.writestr(item, content)
    manifest = {'schemaVersion': 'ati-local-agent-client.v1', 'sourceRepository': 'winglight/ati-worker-orchestrator',
                'sourceCommit': revision, 'sources': inventory, 'filename': wheel.name,
                'sha256': hashlib.sha256(wheel.read_bytes()).hexdigest()}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return wheel


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(build(args.source.resolve(), args.output.resolve()))
