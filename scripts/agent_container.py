"""Container-only outbound Agent. No host daemon, model key handling or trading grant."""
from __future__ import annotations
import argparse
import asyncio
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit


async def cloud_identity(directory, gateway):
    import httpx
    path = Path(directory) / 'installation.json'
    value = json.loads(path.read_text()) if path.is_file() else {}
    if not value.get('credential'):
        return {'bound': False}
    async with httpx.AsyncClient(base_url=gateway, timeout=30) as client:
        response = await client.get('/cloud/local-execution/identity',
            headers={'Authorization': 'Bearer ' + value['credential']})
        if response.status_code != 200:
            raise ValueError('AGENT_CLOUD_ACCOUNT_VERIFICATION_FAILED: HTTP ' + str(response.status_code))
    result = response.json()
    if result.get('environmentId') != value.get('environmentId') or not result.get('email'):
        raise ValueError('AGENT_CLOUD_IDENTITY_INVALID')
    return {'bound': True, **result}


def provider():
    value = os.environ.get('ATI_AGENT_PROVIDER', 'codex')
    if value not in {'codex', 'claude'}:
        raise ValueError('INVALID_AGENT_PROVIDER')
    return value


def auth_command(login=False):
    if provider() == 'codex':
        return ['codex', 'login', '--device-auth'] if login else ['codex', 'login', 'status']
    return ['claude', 'auth', 'login'] if login else ['claude', 'auth', 'status']


def auth_status():
    return subprocess.run(auth_command(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.ati-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def state_path():
    return Path(os.environ.get('ATI_AGENT_STATUS_FILE', '/run/ati-agent/status.json'))


def report(state, **extra):
    atomic(state_path(), json.dumps({'state': state, 'provider': provider(), **extra}))


def status():
    try:
        value = json.loads(state_path().read_text())
    except (OSError, ValueError):
        value = {'state': 'starting', 'provider': provider()}
    value['online'] = value.get('state') == 'online' and time.time() - value.get('heartbeatAt', 0) < 90
    return value


def import_auth():
    # The one-off container sees only this native auth file, never the host HOME,
    # model configuration, plugins, MCP settings, or the Docker socket.
    source = Path('/run/ati-native-auth')
    value = source.read_bytes()
    if len(value) > 2 * 1024 * 1024 or not isinstance(json.loads(value), dict):
        raise ValueError('INVALID_NATIVE_AUTH_FILE')
    target = (Path(os.environ['CODEX_HOME']) / 'auth.json' if provider() == 'codex'
              else Path(os.environ['CLAUDE_CONFIG_DIR']) / '.credentials.json')
    if not target.is_file():
        atomic(target, value.decode())


async def run(connect_only=False):
    import httpx
    from worker_orchestrator.local_connector import Connector
    adapter = 'worker_orchestrator.' + provider() + '_agent_adapter'
    importlib.import_module(adapter)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    class Agent(Connector):
        async def connect(self, *, open_browser=False):
            from worker_orchestrator.local_connector import atomic_json
            account = os.environ.get('ATI_AGENT_CLOUD_ACCOUNT', '').strip().casefold()
            if not account:
                raise ValueError('AGENT_CLOUD_ACCOUNT_REQUIRED')
            if not self.identity.get('credential'):
                if self.identity.get('migrationRuntimeToken'):
                    raise ValueError('AGENT_ACCOUNT_CONFIRMATION_REQUIRED')
                if self.identity.get('accountEmail') not in {None, account}:
                    raise ValueError('AGENT_CLOUD_ACCOUNT_MISMATCH')
                if self.identity.get('bindToken') and self.identity.get('accountEmail') != account:
                    raise ValueError('AGENT_ACCOUNT_CONFIRMATION_REQUIRED')
                if not self.identity.get('bindToken'):
                    import hashlib
                    response = await self.http.post('/cloud/local-bind-requests', json={
                        'installId': self.identity['installId'], 'fingerprint': self.identity['fingerprint'],
                        'name': 'ATI Agent ' + os.uname().nodename,
                        'metadata': {'delivery': 'daemon', 'accountEmail': account,
                            'proofHash': hashlib.sha256(self.identity['proof'].encode()).hexdigest()}})
                    response.raise_for_status()
                    result = response.json()
                    self.identity.update(accountEmail=account, bindingUrl=result['studioUrl'],
                        bindToken=parse_qs(urlsplit(result['studioUrl']).query)['bindRequest'][0],
                        bindExpiresAt=result['expiresAt'])
                    atomic_json(self.directory / 'installation.json', self.identity)
            await super().connect(open_browser=False)
            identity = await cloud_identity(self.directory, self.gateway)
            if identity.get('email', '').casefold() != account:
                raise ValueError('AGENT_CLOUD_ACCOUNT_MISMATCH')
            print('ATI cloud account / 云端账号: ' + identity['email'], flush=True)
            return self.identity

        async def trading_grant(self):
            return None

        async def post(self, path, payload, **kwargs):
            try:
                result = await super().post(path, payload, **kwargs)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403}:
                    raise RuntimeError('AGENT_AUTHORIZATION_REJECTED') from None
                raise
            if path == 'heartbeat':
                report('online', heartbeatAt=time.time())
            return result

    connector = Agent(os.environ.get('ATI_CLOUD_GATEWAY_URL', 'https://ati.broyustudio.com'),
                      os.environ['ATI_ENVIRONMENT_DIR'], component='AGENT',
                      agent_command=[sys.executable, '-m', adapter])
    try:
        if connect_only:
            await connector.connect(open_browser=False)
            print('ATI environment connected / ATI 环境已连接。', flush=True)
            return
        report('starting')
        if not auth_status():
            report('blocked', reason='AGENT_SIGN_IN_REQUIRED')
            await stop.wait()
            return
        task = asyncio.create_task(connector.run())
        stopping = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({task, stopping}, return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                try:
                    await task
                except (ValueError, RuntimeError):
                    report('blocked', reason='AGENT_CONNECTION_REJECTED')
                    # Do not loop on rejected authorization or overwrite grants.
                    await stop.wait()
        finally:
            task.cancel()
            stopping.cancel()
            await asyncio.gather(task, stopping, return_exceptions=True)
    finally:
        await connector.http.aclose()
        if not connect_only:
            report('stopped')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['run', 'connect', 'inspect-account', 'auth-status', 'auth-login', 'import-auth', 'status', 'version'])
    args = parser.parse_args()
    os.umask(0o077)
    Path(os.environ['HOME']).mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.action == 'inspect-account':
        print(json.dumps(asyncio.run(cloud_identity(os.environ['ATI_ENVIRONMENT_DIR'], os.environ['ATI_CLOUD_GATEWAY_URL']))))
        return 0
    if args.action == 'status':
        value = status()
        print(json.dumps(value))
        return 0 if value['online'] else 1
    if args.action == 'version':
        return subprocess.run([provider(), '--version']).returncode
    if args.action == 'auth-status':
        return 0 if auth_status() else 1
    if args.action == 'auth-login':
        return subprocess.run(auth_command(login=True)).returncode
    if args.action == 'import-auth':
        import_auth()
        return 0
    asyncio.run(run(connect_only=args.action == 'connect'))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError):
        print('Agent container setup failed; check container status and native CLI login.', file=sys.stderr)
        raise SystemExit(1)
