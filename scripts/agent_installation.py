"""Optional host Agent installer. Saved configuration is data; credentials stay native."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from agent_service import control
from connector_installer_lock import acquire

def agent_package(root):
    if os.environ.get('ATI_AGENT_PACKAGE_URL'):
        return os.environ['ATI_AGENT_PACKAGE_URL']
    manifest = json.loads((root / 'agent-runtime/manifest.json').read_text())
    filename = manifest['filename']
    if Path(filename).name != filename or not filename.endswith('.whl'):
        raise ValueError('Invalid bundled Agent client filename')
    wheel = root / 'agent-runtime' / filename
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != manifest['sha256']:
        raise ValueError('Bundled Agent client checksum mismatch')
    return str(wheel)


def saved(path):
    result = {}
    for line in path.read_text().splitlines() if path.is_file() else []:
        key, sep, value = line.partition('=')
        if not sep or key not in {'ATI_ENVIRONMENT_DIR', 'ATI_CLOUD_GATEWAY_URL', 'ATI_AGENT_HOME', 'ATI_AGENT_PROVIDER'}:
            continue
        parts = shlex.split(value)
        if len(parts) != 1 or key in result or any(c in parts[0] for c in ('\0', '\n', '\r')):
            raise ValueError('Invalid saved installer configuration: ' + path.name)
        result[key] = parts[0]
    return result


def environment(root, requested=''):
    values = set()
    for filename in ('.research.env', '.trading-connector.env', '.agent.env'):
        value = saved(root / filename).get('ATI_ENVIRONMENT_DIR')
        if value:
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ValueError('Saved environment directory must be absolute')
            values.add(str(path.resolve()))
    if requested:
        path = Path(requested).expanduser()
        if not path.is_absolute():
            raise ValueError('Requested environment directory must be absolute')
        values.add(str(path.resolve()))
    if len(values) > 1:
        raise ValueError('Saved components use different environment directories; no binding was changed')
    return next(iter(values), str(Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'ati/environment'))


def gateway_url(root, requested=''):
    values = {value.rstrip('/') for filename in ('.research.env', '.trading-connector.env', '.agent.env')
              if (value := saved(root / filename).get('ATI_CLOUD_GATEWAY_URL'))}
    if requested:
        values.add(requested.rstrip('/'))
    if len(values) > 1:
        raise ValueError('Saved components use different Gateways; no credential was sent')
    gateway = next(iter(values), 'https://ati.broyustudio.com')
    parsed = urlsplit(gateway)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or not (parsed.scheme == 'https' or parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1'}):
        raise ValueError('Agent Gateway must be HTTPS, or a local HTTP Gateway')
    return gateway


def restore_identity_owner(directory):
    # Only sudo's invoking user can receive access, only to known identity files.
    uid = int(os.environ.get('SUDO_UID', '0'))
    gid = int(os.environ.get('SUDO_GID', '0'))
    if os.getuid() != 0 or uid <= 0:
        raise ValueError('Identity permission setup requires the original installing user through sudo')
    directory = Path(directory)
    identity = directory / 'installation.json'
    if identity.is_symlink() or not identity.is_file():
        raise ValueError('No existing connector identity; directory ownership was not changed')
    value = json.loads(identity.read_text())
    if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k] for k in ('installId', 'fingerprint', 'proof')):
        raise ValueError('Invalid connector identity; directory ownership was not changed')
    for path in (directory, directory / '.identity.lock', directory / '.connector-install.lock', directory / 'installation.json'):
        if not path.exists() and not path.is_symlink():
            continue
        item = path.lstat()
        if item.st_uid not in {0, uid} or path.is_symlink():
            raise ValueError('Identity belongs to another user; no ownership was changed')
        if not (stat.S_ISDIR(item.st_mode) if path == directory else stat.S_ISREG(item.st_mode)):
            raise ValueError('Invalid identity file type')
    for path in (directory, directory / '.identity.lock', directory / '.connector-install.lock', directory / 'installation.json'):
        if path.exists():
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fchown(fd, uid, gid)
                os.fchmod(fd, 0o700 if path == directory else 0o600)
            finally:
                os.close(fd)


def ensure_identity_access(directory, root, non_interactive):
    directory = Path(directory)
    paths = (directory, directory / '.identity.lock', directory / '.connector-install.lock', directory / 'installation.json')
    if all(not p.exists() or os.access(p, os.R_OK | os.W_OK | (os.X_OK if p == directory else 0)) for p in paths):
        return
    if not shutil.which('sudo'):
        raise ValueError('The current user cannot access the existing environment; no new binding was created')
    print('Existing Docker identity needs access for this user / 需要系统权限让当前用户读取原绑定；身份内容不变。', flush=True)
    command(['sudo', *(['-n'] if non_interactive else []), sys.executable, Path(__file__).resolve(),
             'identity-access', '--root', root, '--requested-state', directory])


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def command(args, **kwargs):
    # Arguments are never evaluated by a shell; output belongs to the installing user.
    subprocess.run([str(x) for x in args], check=True, **kwargs)


def download_script(url, env=None, shell='/bin/sh'):
    with tempfile.TemporaryDirectory(prefix='ati-agent-download-') as directory:
        target = Path(directory) / 'install.sh'
        command(['curl', '-fsSL', url, '-o', target])
        command([shell, target], env=env)


def python_supported(path):
    if not path:
        return False
    result = subprocess.run([str(path), '-c', 'import sys; raise SystemExit(sys.version_info < (3, 11))'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def prepare_python(home):
    requested = os.environ.get('ATI_AGENT_PYTHON')
    if requested:
        if not python_supported(requested):
            raise ValueError('ATI_AGENT_PYTHON must point to Python 3.11 or newer')
        executable = requested
    else:
        executable = next((path for path in [sys.executable, *(shutil.which('python3.' + str(v)) for v in (13, 12, 11)), shutil.which('python3')]
                           if path and python_supported(path)), None)
    venv = home / 'venv'
    if executable:
        if not (venv / 'bin/python').is_file():
            command([executable, '-m', 'venv', venv])
    else:
        uv = shutil.which('uv') or str(home / 'tools/uv')
        if not Path(uv).is_file():
            download_script('https://astral.sh/uv/install.sh', {**os.environ, 'UV_UNMANAGED_INSTALL': str(home / 'tools')})
        command([uv, 'venv', '--python', '3.11', '--seed', venv])
    python = venv / 'bin/python'
    if not python_supported(python):
        raise ValueError('The Agent Python environment is unavailable')
    return python


def prepare_codex(non_interactive, require_login=True, env=None):
    codex = shutil.which('codex')
    if not codex:
        for candidate in (Path.home() / '.local/bin/codex', Path.home() / '.cargo/bin/codex'):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                codex = str(candidate)
                break
    if not codex:
        print('Installing Codex CLI / 正在安装 Codex CLI…', flush=True)
        download_script('https://chatgpt.com/codex/install.sh', env=env)
        codex = shutil.which('codex')
        if not codex:
            codex = next((str(p) for p in (Path.home() / '.local/bin/codex', Path.home() / '.cargo/bin/codex')
                          if p.is_file() and os.access(p, os.X_OK)), None)
    if not codex:
        raise ValueError('Codex installation did not provide a runnable CLI')
    if not require_login:
        return str(Path(codex).absolute())
    status = subprocess.run([codex, 'login', 'status'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if status.returncode:
        if non_interactive:
            raise ValueError('Codex sign-in is required. Rerun the one-click installer interactively; no Agent was started')
        print('Sign in to Codex in the browser / 请按浏览器提示登录自己的 Codex 账号。', flush=True)
        command([codex, 'login'], env=env)
        command([codex, 'login', 'status'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return str(Path(codex).absolute())


def select_provider(prior, requested=None, non_interactive=False):
    selected = requested or os.environ.get('ATI_AGENT_PROVIDER') or prior.get('ATI_AGENT_PROVIDER', 'codex')
    if selected not in {'codex', 'claude'}:
        raise ValueError('Agent provider must be codex or claude')
    if not requested and not os.environ.get('ATI_AGENT_PROVIDER') and not non_interactive:
        answer = input('Local Agent / 选择本地 Agent: 1) Codex  2) Claude Code [' + ('2' if selected == 'claude' else '1') + ']: ').strip()
        selected = {'1': 'codex', '2': 'claude', 'codex': 'codex', 'claude': 'claude', '': selected}.get(answer)
        if not selected:
            raise ValueError('Invalid local Agent selection')
    return selected


def prepare_claude(non_interactive, require_login=True, env=None):
    env = dict(os.environ if env is None else env)
    candidate = Path.home() / '.local/bin/claude'
    claude = shutil.which('claude') or (str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None)
    required = ('--safe-mode', '--tools', '--strict-mcp-config', '--no-session-persistence', '--input-format', '--output-format')
    def supported(binary):
        if not binary:
            return False
        result = subprocess.run([binary, '--help'], env=env, capture_output=True, text=True)
        return result.returncode == 0 and all(flag in result.stdout for flag in required)
    if not supported(claude):
        print('Installing/updating Claude Code / 正在安装或更新 Claude Code…', flush=True)
        download_script('https://claude.ai/install.sh', env=env, shell='/bin/bash')
        claude = str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else shutil.which('claude')
    if not supported(claude):
        raise ValueError('Claude Code installation must support safe-mode and JSON print mode')
    if require_login:
        status = subprocess.run([claude, 'auth', 'status'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if status.returncode:
            if non_interactive:
                raise ValueError('Claude Code sign-in is required. Rerun the one-click installer interactively; no Agent was started')
            print('Sign in to Claude Code in the browser / 请按浏览器提示登录自己的 Claude 账号。', flush=True)
            command([claude, 'auth', 'login'], env=env)
            command([claude, 'auth', 'status'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return str(Path(claude).absolute())


def service_status(config):
    try:
        return control(config, 'status')
    except (OSError, ValueError):
        return {'online': False, 'running': False}


def stop_service(config):
    manager = config.get('manager', 'process')
    if manager == 'launchd':
        subprocess.run(['launchctl', 'bootout', 'gui/' + str(os.getuid()), config['serviceFile']], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif manager == 'systemd':
        command(['systemctl', '--user', 'stop', config['label'] + '.service'])
    try:
        control(config, 'stop')
    except OSError:
        return
    for _ in range(100):
        try:
            control(config, 'status')
        except OSError:
            return
        time.sleep(0.1)
    raise ValueError('Agent did not finish stopping; its Python environment was not replaced')


def start_service(config, config_path):
    if service_status(config).get('pid'):
        return
    argv = [config['python'], config['runner'], str(config_path)]
    env = {**os.environ, **config.get('environment', {}), 'PATH': config['path']}
    log = Path(config['log'])
    log.touch(mode=0o600, exist_ok=True)
    log.chmod(0o600)
    mode = os.environ.get('ATI_AGENT_SERVICE_MODE', 'auto')
    if mode not in {'auto', 'process'}:
        raise ValueError('Invalid Agent service mode')
    if mode == 'auto' and sys.platform == 'darwin' and shutil.which('launchctl'):
        service = Path.home() / 'Library/LaunchAgents' / (config['label'] + '.plist')
        write(service, plistlib.dumps({'Label': config['label'], 'ProgramArguments': argv,
              'EnvironmentVariables': {**config.get('environment', {}), 'PATH': config['path']}, 'RunAtLoad': True,
              'WorkingDirectory': str(config_path.parent), 'StandardOutPath': str(log), 'StandardErrorPath': str(log)}).decode())
        result = subprocess.run(['launchctl', 'bootstrap', 'gui/' + str(os.getuid()), str(service)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            config.update(manager='launchd', serviceFile=str(service))
    elif mode == 'auto' and shutil.which('systemctl') and subprocess.run(['systemctl', '--user', 'show-environment'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        service = Path.home() / '.config/systemd/user' / (config['label'] + '.service')
        # systemd has its own quoting and percent expansion; no shell interpolation.
        quote = lambda x: '"' + str(x).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('\n', '\\n') + '"'
        write(service, '[Unit]\nDescription=ATI local Agent\n[Service]\nType=simple\nExecStart=' + ' '.join(quote(x) for x in argv) + '\nEnvironment=' + quote('PATH=' + config['path']) + ''.join('\nEnvironment=' + quote(k + '=' + v) for k, v in config.get('environment', {}).items()) + '\nWorkingDirectory=' + quote(config_path.parent) + '\nUMask=0077\nStandardOutput=' + quote('file:' + str(log)) + '\nStandardError=inherit\nRestart=no\n[Install]\nWantedBy=default.target\n')
        command(['systemctl', '--user', 'daemon-reload'])
        command(['systemctl', '--user', 'enable', '--now', service.name])
        config.update(manager='systemd', serviceFile=str(service))
    if config.get('manager') not in {'launchd', 'systemd'}:
        with log.open('ab') as stream:
            subprocess.Popen(argv, env=env, cwd=config_path.parent, stdin=subprocess.DEVNULL,
                             stdout=stream, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        config['manager'] = 'process'
    write(config_path, json.dumps(config))


def install(root, non_interactive=False, no_connect=False, provider=None):
    prior = saved(root / '.agent.env')
    state = environment(root, os.environ.get('ATI_ENVIRONMENT_DIR', ''))
    gateway = gateway_url(root, os.environ.get('ATI_CLOUD_GATEWAY_URL', ''))
    key = hashlib.sha256(state.encode()).hexdigest()[:16]
    base = os.environ.get('ATI_AGENT_HOME') or prior.get('ATI_AGENT_HOME')
    home = Path(base) if base else Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'ati/agent' / key
    home = home.expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path = home / 'service.json'
    if no_connect and config_path.is_file():
        print('Existing Agent retained; connection and startup changes skipped / 保留已有 Agent，不更改连接或运行状态。')
        return
    selected = select_provider(prior, provider, non_interactive)
    previous_config = json.loads(config_path.read_text()) if config_path.is_file() else {}
    service_env = {key: os.environ.get(key, previous_config.get('environment', {}).get(key))
                   for key in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR')}
    service_env = {key: value for key, value in service_env.items() if value is not None}
    cli = (prepare_claude(non_interactive, require_login=not no_connect, env={**os.environ, **service_env})
           if selected == 'claude' else prepare_codex(non_interactive, require_login=not no_connect, env={**os.environ, **service_env}))
    ensure_identity_access(state, root, non_interactive)
    lock = acquire(Path(state))
    try:
        if config_path.exists():
            stop_service(json.loads(config_path.read_text()))
        python = prepare_python(home)
        package = agent_package(root)
        command([python, '-m', 'pip', 'install', '--upgrade', package])
        config = {'python': str(python), 'runner': str(root / 'scripts/agent_service.py'), 'gateway': gateway, 'provider': selected,
                  'stateDir': state, 'label': 'com.broyustudio.ati-agent.' + key,
                  'socket': '/tmp/ati-agent-' + str(os.getuid()) + '-' + key + '.sock',
                  'controlToken': secrets.token_urlsafe(32), 'log': str(home / 'agent.log'),
                  'environment': service_env,
                  'path': str(Path(cli).parent) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin')}
        write(config_path, json.dumps(config))
        write(root / '.agent.env', ''.join(k + '=' + shlex.quote(v) + '\n' for k, v in {
              'ATI_ENVIRONMENT_DIR': state, 'ATI_CLOUD_GATEWAY_URL': gateway, 'ATI_AGENT_HOME': str(home), 'ATI_AGENT_PROVIDER': selected}.items()))
        if no_connect:
            print('Agent installed; Studio linking and startup were explicitly skipped / 已安装，按所选选项跳过连接与启动。')
            return
        print('Connecting ATI environment / 正在连接 ATI 云账号（已有有效绑定会自动复用）…', flush=True)
        command([python, '-m', 'worker_orchestrator.local_connector', 'connect', '--gateway', gateway, '--state-dir', state, '--component', 'AGENT'], env={**os.environ, 'PATH': config['path']})
        start_service(config, config_path)
    finally:
        os.close(lock)
    deadline = time.monotonic() + float(os.environ.get('ATI_AGENT_START_TIMEOUT', '45'))
    while time.monotonic() < deadline:
        if service_status(config).get('online'):
            print('Agent connected / 本地 Agent 已上线。回到 Studio → 执行设置，选择在线 Agent 设备。')
            print('Background mode / 后台运行方式: ' + config['manager'])
            print('Local Agent / 本地 Agent: ' + selected)
            return
        time.sleep(0.2)
    raise ValueError('Agent installed but not online; background diagnostics: ' + config['log'] + '. Rerun the installer/status to recover; no online status was assumed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['environment', 'gateway', 'identity-access', 'install', 'upgrade', 'start', 'stop', 'status'])
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--requested-state', default='')
    parser.add_argument('--non-interactive', action='store_true')
    parser.add_argument('--no-connect', action='store_true')
    parser.add_argument('--provider', choices=['codex', 'claude'])
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.action == 'identity-access':
            if environment(root, args.requested_state) != str(Path(args.requested_state).resolve()):
                raise ValueError('Unverified environment directory')
            restore_identity_owner(args.requested_state)
            return 0
        if args.action == 'environment':
            print(environment(root, args.requested_state))
            return 0
        if args.action == 'gateway':
            print(gateway_url(root, os.environ.get('ATI_CLOUD_GATEWAY_URL', '')))
            return 0
        if args.action in {'install', 'upgrade'}:
            install(root, args.non_interactive, args.no_connect, args.provider)
            return 0
        prior = saved(root / '.agent.env')
        if not prior.get('ATI_AGENT_HOME'):
            raise ValueError('Agent is not installed; rerun the one-click installer and select Agent')
        path = Path(prior['ATI_AGENT_HOME']) / 'service.json'
        config = json.loads(path.read_text())
        if args.action == 'stop':
            stop_service(config)
            print('Agent stopped / Agent 已停止。')
        elif args.action == 'start':
            start_service(config, path)
            print('Agent started; check status / 已启动，请查看状态。')
        else:
            value = service_status(config)
            print('Agent online / Agent 在线' if value.get('online') else 'Agent is not online / Agent 未在线')
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print('Agent installation: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
