"""User-owned Agent service: native Connector, authenticated local control, no trading grant."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import importlib
import signal
import stat
import sys
import time


def adapter_module(config):
    provider = config.get('provider', 'codex')
    if provider not in {'codex', 'claude'}:
        raise ValueError('Invalid saved Agent provider')
    return 'worker_orchestrator.' + provider + '_agent_adapter'


def control(config, action, timeout=2):
    import socket
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(config['socket'])
        client.sendall((json.dumps({'action': action, 'token': config['controlToken']}) + '\n').encode())
        data = b''
        while b'\n' not in data:
            part = client.recv(4096)
            if not part or len(data) > 8192:
                raise ValueError('Invalid local Agent response')
            data += part
    result = json.loads(data.split(b'\n', 1)[0])
    if result.get('token') != config['controlToken']:
        raise ValueError('Local Agent ownership mismatch')
    return result


async def serve(config):
    import httpx
    from worker_orchestrator.local_connector import Connector
    adapter = adapter_module(config)
    importlib.import_module(adapter)
    state = {'online': False, 'heartbeatAt': 0.0, 'pid': os.getpid()}
    stop = asyncio.Event()

    class Agent(Connector):
        async def trading_grant(self):
            # AI installation and execution never acquire a trading entitlement.
            return None

        async def post(self, path, payload, **kwargs):
            try:
                result = await super().post(path, payload, **kwargs)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403}:
                    # A rejected AI connection must not overwrite trading grants.
                    raise RuntimeError('AGENT_AUTHORIZATION_REJECTED') from None
                raise
            if path == 'heartbeat':
                if not state['online']:
                    print('Agent connected / Agent 已连接', flush=True)
                state.update(online=True, heartbeatAt=time.time())
            return result

    async def request(reader, writer):
        try:
            value = json.loads(await asyncio.wait_for(reader.readline(), 2))
            if value.get('token') != config['controlToken']:
                return
            if value.get('action') == 'stop':
                stop.set()
            elif value.get('action') != 'status':
                return
            current = dict(state, token=config['controlToken'])
            current['online'] = state['online'] and time.time() - state['heartbeatAt'] < 90
            writer.write((json.dumps(current) + '\n').encode())
            await writer.drain()
        except (ValueError, OSError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    path = Path(config['socket'])
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode) or path.lstat().st_uid != os.getuid():
            raise ValueError('Unverified Agent control socket; no existing process changed')
        try:
            control(config, 'status')
        except OSError:
            path.unlink()
        else:
            raise ValueError('Agent is already running')
    server = await asyncio.start_unix_server(request, path=str(path), limit=8192)
    path.chmod(0o600)
    connector = Agent(config['gateway'], config['stateDir'], component='AGENT',
                      agent_command=[config['python'], '-m', adapter])
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    task = asyncio.create_task(connector.run())
    stopping = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({task, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            await task
    finally:
        state['online'] = False
        task.cancel()
        stopping.cancel()
        await asyncio.gather(task, stopping, return_exceptions=True)
        await connector.http.aclose()
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    os.umask(0o077)
    try:
        asyncio.run(serve(config))
    except Exception as exc:
        # Operation inputs and model credentials must never enter installer logs.
        print('Agent service stopped (' + type(exc).__name__ + '). Rerun the installer to inspect/reconnect.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
