"""Container-only outbound Agent. No host daemon, model key handling or trading grant."""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
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


async def local_inference(adapter, request):
    # Use the same non-interactive sandboxed CLI adapter as Studio.
    packet = {'prompt': 'Respond to the local AI request below.',
              'variables': {'messages': request['messages']}}
    with tempfile.TemporaryDirectory(prefix='ati-trading-ai-') as work:
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-m', adapter, cwd=work, start_new_session=True,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await process.communicate(json.dumps(packet, ensure_ascii=False).encode())
            if process.returncode:
                return {'error': 'LOCAL_AGENT_REQUEST_FAILED'}
            if len(output) >= 1_000_000:
                return {'error': 'LOCAL_AGENT_OUTPUT_TOO_LARGE'}
            try:
                text = json.loads(output)['text']
            except (ValueError, KeyError, TypeError):
                return {'error': 'LOCAL_AGENT_INVALID_RESPONSE'}
            return {'text': text} if isinstance(text, str) else {'error': 'LOCAL_AGENT_INVALID_RESPONSE'}
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()


@asynccontextmanager
async def local_agent_socket(path, adapter, *, infer=local_inference):
    if not path:
        yield
        return
    socket_path = Path(path)
    active = False

    async def handle(reader, writer):
        nonlocal active
        try:
            try:
                async with asyncio.timeout(2):
                    payload = json.loads(await reader.readline())
            except (ValueError, TimeoutError):
                return
            if not isinstance(payload, dict) or payload.get('model') != provider():
                result = {'error': 'LOCAL_AGENT_MODEL_MISMATCH'}
            elif active:
                result = {'error': 'LOCAL_AGENT_BUSY'}
            else:
                messages = payload.get('messages')
                timeout = payload.get('timeout')
                if (type(timeout) is not int or not 1 <= timeout <= 300 or not isinstance(messages, list)
                        or not messages or any(not isinstance(message, dict)
                        or message.get('role') not in ('system', 'user', 'assistant')
                        or not isinstance(message.get('content'), str) for message in messages)):
                    return
                active = True
                work = asyncio.create_task(infer(adapter, payload))
                disconnected = asyncio.create_task(reader.read(1))
                try:
                    done, _ = await asyncio.wait({work, disconnected}, timeout=timeout,
                                                  return_when=asyncio.FIRST_COMPLETED)
                    if disconnected in done:
                        return
                    result = work.result() if work in done else {'error': 'LOCAL_AGENT_TIMEOUT'}
                except (OSError, RuntimeError):
                    result = {'error': 'LOCAL_AGENT_REQUEST_FAILED'}
                finally:
                    work.cancel()
                    disconnected.cancel()
                    await asyncio.gather(work, disconnected, return_exceptions=True)
                    active = False
            encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()
            if len(encoded) >= 1_000_000:
                encoded = b'{"error":"LOCAL_AGENT_OUTPUT_TOO_LARGE"}'
            writer.write(encoded + b'\n')
            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    # The volume is mounted only by this Agent and the local strategy service.
    # Socket access is granted by that mount; no port, key or device credential.
    if socket_path.is_socket():
        try:
            async with asyncio.timeout(1):
                _, peer = await asyncio.open_unix_connection(path)
        except (ConnectionRefusedError, FileNotFoundError):
            socket_path.unlink(missing_ok=True)
        else:
            peer.close()
            await peer.wait_closed()
            raise RuntimeError('LOCAL_AGENT_SOCKET_ALREADY_ACTIVE')
    server = await asyncio.start_unix_server(handle, path=path, limit=256_000)
    try:
        socket_path.chmod(0o666)
        async with server:
            yield
    finally:
        server.close()
        await server.wait_closed()
        socket_path.unlink(missing_ok=True)


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
        async with local_agent_socket(os.environ.get('ATI_LOCAL_AGENT_SOCKET'), adapter), interactive_agent(connector):
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


@asynccontextmanager
async def interactive_agent(connector):
    """The CLI and protocol journal live only inside the existing Agent container."""
    path=os.environ.get('ATI_AGENT_SESSION_SOCKET','')
    reverse=os.environ.get('ATI_AGENT_REVERSE_URL','')
    if not (path or reverse):
        yield
        return
    from worker_orchestrator.agent_session_store import SessionJournal
    from worker_orchestrator.agent_sessions import NativeSessionRuntime
    from worker_orchestrator.claude_sessions import ClaudeSessionRuntime
    from worker_orchestrator.agent_session_channel import SessionChannel,stream_channel,reverse_channel
    journal=SessionJournal(Path(os.environ.get('ATI_AGENT_SESSION_JOURNAL','/var/lib/ati-agent/session-protocol.db')))
    runtime_type=NativeSessionRuntime if provider()=='codex' else ClaudeSessionRuntime
    runtime=runtime_type(journal,Path('/var/lib/ati-agent/workspace'))
    async def certificate(challenge,binding):
        if not connector.identity.get('credential'):raise ValueError('AGENT_CLOUD_BINDING_REQUIRED')
        response=await connector.http.post('/cloud/local-execution/agent-session-certificate',
            headers={'Authorization':'Bearer '+connector.identity['credential']},json=dict(challenge=challenge,
                targetEnvironmentId=binding['environmentId'],targetInstallationId=binding['installationId']))
        if response.status_code!=200:raise ValueError('AGENT_CLOUD_DEVICE_PROOF_REJECTED')
        return response.json()['certificate']
    def keys():
        result=json.loads(os.environ.get('ATI_PRODUCT_GRANT_PUBLIC_KEYS') or '{}')
        if not isinstance(result,dict) or not result:raise ValueError('AGENT_PRODUCT_TRUST_UNCONFIGURED')
        return result
    server=None;reverse_task=None;channel=None;handlers=set()
    try:
        await runtime.start()
        channel=SessionChannel(runtime,certificate,keys)
        if path:
            socket_path=Path(path)
            if socket_path.is_socket():
                try:
                    _,peer=await asyncio.open_unix_connection(path)
                except (ConnectionError,OSError):socket_path.unlink()
                else:
                    peer.close();await peer.wait_closed();raise ValueError('AGENT_SESSION_SOCKET_IN_USE')
            async def handle(reader,writer):
                task=asyncio.current_task();handlers.add(task)
                try:await stream_channel(channel,reader,writer)
                finally:handlers.discard(task)
            server=await asyncio.start_unix_server(handle,path=path,limit=512001)
            socket_path.chmod(0o660)
        if reverse:reverse_task=asyncio.create_task(reverse_channel(channel,reverse))
        yield
    finally:
        if server:
            server.close();await server.wait_closed();Path(path).unlink(missing_ok=True)
        if reverse_task:
            reverse_task.cancel();await asyncio.gather(reverse_task,return_exceptions=True)
        for task in tuple(handlers):task.cancel()
        await asyncio.gather(*handlers,return_exceptions=True)
        await runtime.close()
        if channel:await asyncio.gather(*channel.inflight,return_exceptions=True)
        journal.close()


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
