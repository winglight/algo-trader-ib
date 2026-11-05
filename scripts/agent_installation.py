"""Transient Docker Agent installer. No host Agent or model runtime is installed."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from connector_installer_lock import acquire

def agent_package(root):
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
        if not sep or key not in {'ATI_ENVIRONMENT_DIR', 'ATI_CLOUD_GATEWAY_URL', 'ATI_AGENT_HOME', 'ATI_AGENT_PROVIDER', 'ATI_AGENT_DATA_DIR', 'ATI_AGENT_IMAGE', 'ATI_LOCAL_UID', 'ATI_LOCAL_GID', 'ATI_AGENT_CPUS', 'ATI_AGENT_MEMORY', 'ATI_AGENT_CLOUD_ACCOUNT'}:
            continue
        parts = shlex.split(value)
        if len(parts) != 1 or key in result or any(c in parts[0] for c in ('\0', '\n', '\r')):
            raise ValueError('Invalid saved installer configuration: ' + path.name)
        result[key] = parts[0]
    return result


def environment(root, requested=''):
    values = set()
    for filename in ('.research.env', '.trading-connector.env', '.agent.env'):
        settings = saved(root / filename)
        # An explicitly selected Agent account may have its own environment.
        if filename == '.agent.env' and settings.get('ATI_AGENT_CLOUD_ACCOUNT'):
            continue
        value = settings.get('ATI_ENVIRONMENT_DIR')
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


def select_cloud_account(prior, requested=None, non_interactive=False):
    value = requested or os.environ.get('ATI_AGENT_CLOUD_ACCOUNT', '')
    if not value and not non_interactive:
        previous = prior.get('ATI_AGENT_CLOUD_ACCOUNT', '')
        print('Select the ATI cloud account shown in Studio. Local/public versions can use different accounts. / 请输入 Studio 云端账号；local/public 版可能绑定不同账号。')
        value = input('ATI cloud account email / 云端账号邮箱' + (' [' + previous + ']' if previous else '') + ': ').strip() or previous
    value = value.strip().casefold()
    if len(value) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
        raise ValueError('Enter a cloud account email; non-interactive installation requires --cloud-account or ATI_AGENT_CLOUD_ACCOUNT')
    return value


def account_environment(root, settings):
    def inspect(directory):
        if not (Path(directory) / 'installation.json').is_file():
            return {'bound': False, 'fresh': True}
        result = checked(root, {**settings, 'ATI_ENVIRONMENT_DIR': directory},
            ['run', '--rm', '-T', 'agent', 'inspect-account'], capture_output=True, text=True)
        return json.loads(result.stdout)
    account = settings['ATI_AGENT_CLOUD_ACCOUNT']
    # First try this installer's explicit Agent selection, then the shared
    # environment. Never relabel, unbind or overwrite a different installation.
    prior = saved(root / '.agent.env')
    candidate = (prior.get('ATI_ENVIRONMENT_DIR') if prior.get('ATI_AGENT_CLOUD_ACCOUNT') == account
                 else settings['ATI_ENVIRONMENT_DIR'])
    identity = inspect(candidate)
    if identity.get('fresh') or identity.get('email', '').casefold() == account:
        return candidate
    base = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share')))
    key = hashlib.sha256((settings['ATI_CLOUD_GATEWAY_URL'] + '\n' + account).encode()).hexdigest()[:24]
    selected = str(base / 'ati/agent-environments' / key)
    identity = inspect(selected)
    if not identity.get('fresh') and identity.get('bound') and identity.get('email', '').casefold() != account:
        raise ValueError('Agent environment belongs to another cloud account; no binding was changed')
    print('Using an Agent environment for ' + account + ' / 使用所选账号的独立 Agent 环境，保留其他版本绑定。', flush=True)
    return selected


def docker_ready():
    command(['docker', 'compose', 'version'], stdout=subprocess.DEVNULL)
    command(['docker', 'info'], stdout=subprocess.DEVNULL)


def compose(root, settings):
    key = hashlib.sha256(settings['ATI_ENVIRONMENT_DIR'].encode()).hexdigest()[:12]
    return ['docker', 'compose', '--project-name', 'ati-local-agent-' + key,
            '--project-directory', str(root), '--env-file', '/dev/null',
            '-f', str(root / 'docker-compose.agent.yml')]


def invoke(root, settings, arguments, **kwargs):
    return subprocess.run(compose(root, settings) + list(map(str, arguments)),
                          env={**os.environ, **settings}, **kwargs)


def checked(root, settings, arguments, **kwargs):
    result = invoke(root, settings, arguments, **kwargs)
    if result.returncode:
        raise ValueError('Docker Agent operation failed: ' + str(arguments[0]))
    return result


def image_fingerprint(root):
    digest = hashlib.sha256()
    for path in (root / 'Dockerfile.local-agent', root / 'scripts/agent_container.py',
                 root / 'agent-runtime/manifest.json', Path(agent_package(root))):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def prepare_image(root, selected, prior):
    requested = os.environ.get('ATI_AGENT_IMAGE')
    if requested:
        command(['docker', 'pull', requested])
        return requested
    revision = image_fingerprint(root)
    platform = subprocess.check_output(['docker', 'info', '--format', '{{.Architecture}}'], text=True).strip()
    architecture = {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'amd64', 'amd64': 'amd64'}.get(platform)
    if not architecture:
        raise ValueError('Agent images require an amd64 or arm64 Docker engine')
    image = 'ati-local-agent:' + selected + '-' + architecture + '-' + revision
    exists = subprocess.run(['docker', 'image', 'inspect', image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if exists.returncode == 0:
        return image
    print('Building isolated Agent image / 正在构建独立 Agent 容器镜像（运行依赖仅装入镜像）…', flush=True)
    # Explicit staging prevents .env, credentials, user data, or other products
    # entering a Docker context or intermediate/cache layer.
    with tempfile.TemporaryDirectory(prefix='ati-agent-image-') as directory:
        stage = Path(directory)
        shutil.copyfile(root / 'Dockerfile.local-agent', stage / 'Dockerfile')
        shutil.copyfile(root / 'scripts/agent_container.py', stage / 'agent_container.py')
        wheel = Path(agent_package(root))
        shutil.copyfile(wheel, stage / 'client.whl')
        (stage / 'client-filename').write_text(wheel.name)
        command(['docker', 'buildx', 'build', '--load', '--platform', 'linux/' + architecture, '--target', selected, '--tag', image,
                 '--label', 'io.ati.agent.content=' + revision, '--file', stage / 'Dockerfile', stage])
    return image


def stop_legacy_host(prior, state):
    """One-way handoff of this installer's legacy daemon, never global CLIs."""
    if not prior.get('ATI_AGENT_HOME'):
        return
    home = Path(prior['ATI_AGENT_HOME']).expanduser().resolve()
    path = home / 'service.json'
    if not path.is_file():
        return
    config = json.loads(path.read_text())
    label = config.get('label', '')
    if config.get('stateDir') != state or not label.startswith('com.broyustudio.ati-agent.'):
        raise ValueError('Unverified legacy host Agent; no process changed')
    manager = config.get('manager')
    if manager == 'launchd':
        subprocess.run(['launchctl', 'bootout', 'gui/' + str(os.getuid()) + '/' + label],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        file = Path(config['serviceFile'])
        if file.is_file():
            import plistlib
            if plistlib.loads(file.read_bytes()).get('Label') != label:
                raise ValueError('Unverified legacy LaunchAgent')
            file.rename(file.with_suffix('.plist.host-disabled'))
    elif manager == 'systemd':
        command(['systemctl', '--user', 'disable', '--now', label + '.service'])
    elif manager != 'process':
        raise ValueError('Unverified legacy Agent manager')
    # Authenticated local shutdown also covers the old detached-process mode.
    import socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(config['socket'])
            client.sendall((json.dumps({'action': 'stop', 'token': config['controlToken']}) + '\n').encode())
            response = json.loads(client.recv(8192))
            if response.get('token') != config['controlToken']:
                raise ValueError('Legacy Agent ownership mismatch')
        for _ in range(100):
            if not Path(config['socket']).exists():
                break
            time.sleep(0.1)
        else:
            raise ValueError('Legacy Agent did not stop; container not started')
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    path.rename(home / 'service.host-disabled.json')


def container_status(root, settings):
    result = invoke(root, settings, ['exec', '-T', 'agent', 'python', '/opt/ati/agent_container.py', 'status'],
                    capture_output=True, text=True)
    try:
        return json.loads(result.stdout)
    except ValueError:
        return {'state': 'stopped', 'online': False}


def authenticate(root, settings, non_interactive):
    quiet = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
    if invoke(root, settings, ['run', '--rm', '-T', 'agent', 'auth-status'], **quiet).returncode == 0:
        return
    selected = settings['ATI_AGENT_PROVIDER']
    source = (Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'auth.json'
              if selected == 'codex' else Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude'))) / '.credentials.json')
    if source.is_file():
        # Native file stays opaque to the installer and is mounted read-only only
        # in the one-off import container. Host HOME/configuration is never mounted.
        checked(root, settings, ['run', '--rm', '-T', '--volume', str(source) + ':/run/ati-native-auth:ro',
                                 'agent', 'import-auth'])
        if invoke(root, settings, ['run', '--rm', '-T', 'agent', 'auth-status'], **quiet).returncode == 0:
            return
    if non_interactive:
        raise ValueError('Container Agent sign-in required. Rerun the installer interactively; no Agent was started')
    print('Sign in inside the Agent container / 按容器中的链接完成所选 Agent 登录，无需宿主机 CLI。', flush=True)
    args = ['run', '--rm'] + ([] if sys.stdin.isatty() else ['-T']) + ['agent', 'auth-login']
    checked(root, settings, args)
    checked(root, settings, ['run', '--rm', '-T', 'agent', 'auth-status'], **quiet)


def start(root, settings):
    checked(root, settings, ['up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'agent'])
    deadline = time.monotonic() + float(os.environ.get('ATI_AGENT_START_TIMEOUT', '90'))
    while time.monotonic() < deadline:
        status = container_status(root, settings)
        if status.get('online'):
            print('Agent container connected / 本地 Agent 容器已上线。回到 Studio → 执行设置选择在线设备。', flush=True)
            return
        if status.get('state') == 'blocked':
            checked(root, settings, ['stop', 'agent'])
            raise ValueError('Agent connection blocked: ' + status.get('reason', 'unknown'))
        time.sleep(1)
    checked(root, settings, ['stop', 'agent'])
    raise ValueError('Agent container did not report online; inspect Docker logs, then rerun the installer')


def install(root, non_interactive=False, no_connect=False, provider=None, cloud_account=None):
    prior = saved(root / '.agent.env')
    if no_connect and prior.get('ATI_AGENT_DATA_DIR'):
        print('Existing container retained; connection/startup changes skipped / 保留现有容器状态。')
        return
    state = environment(root, os.environ.get('ATI_ENVIRONMENT_DIR', ''))
    gateway = gateway_url(root, os.environ.get('ATI_CLOUD_GATEWAY_URL', ''))
    selected = select_provider(prior, provider, non_interactive)
    account = select_cloud_account(prior, cloud_account, non_interactive)
    docker_ready()
    image = prepare_image(root, selected, prior)
    key = hashlib.sha256(state.encode()).hexdigest()[:16]
    base = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'ati/agent' / key
    data = Path(os.environ.get('ATI_AGENT_DATA_DIR') or prior.get('ATI_AGENT_DATA_DIR') or base / 'container').expanduser()
    if not data.is_absolute():
        raise ValueError('Agent data directory must be absolute')
    settings = {'ATI_ENVIRONMENT_DIR': state, 'ATI_CLOUD_GATEWAY_URL': gateway, 'ATI_AGENT_PROVIDER': selected,
                'ATI_AGENT_CLOUD_ACCOUNT': account,
                'ATI_AGENT_IMAGE': image, 'ATI_AGENT_DATA_DIR': str(data.resolve()),
                'ATI_LOCAL_UID': str(os.getuid()), 'ATI_LOCAL_GID': str(os.getgid()),
                'ATI_AGENT_CPUS': os.environ.get('ATI_AGENT_CPUS') or prior.get('ATI_AGENT_CPUS', '2'),
                'ATI_AGENT_MEMORY': os.environ.get('ATI_AGENT_MEMORY') or prior.get('ATI_AGENT_MEMORY', '4g')}
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not no_connect:
        state = account_environment(root, settings)
        settings['ATI_ENVIRONMENT_DIR'] = state
    ensure_identity_access(state, root, non_interactive)
    lock = acquire(Path(state))
    try:
        if no_connect:
            write(root / '.agent.env', ''.join(key + '=' + shlex.quote(value) + '\n' for key, value in settings.items()))
            print('Agent image prepared; linking and startup skipped / 已准备容器镜像，跳过登录、绑定与启动。')
            return
        authenticate(root, settings, non_interactive)
        stop_legacy_host(prior, state)
        checked(root, settings, ['run', '--rm', '-T', 'agent', 'connect'])
        if prior.get('ATI_AGENT_DATA_DIR'):
            checked(root, {**prior, 'ATI_AGENT_CLOUD_ACCOUNT': account}, ['stop', 'agent'], stdout=subprocess.DEVNULL)
        write(root / '.agent.env', ''.join(key + '=' + shlex.quote(value) + '\n' for key, value in settings.items()))
        start(root, settings)
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['environment', 'gateway', 'identity-access', 'install', 'upgrade', 'start', 'stop', 'status'])
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--requested-state', default='')
    parser.add_argument('--non-interactive', action='store_true')
    parser.add_argument('--no-connect', action='store_true')
    parser.add_argument('--provider', choices=['codex', 'claude'])
    parser.add_argument('--cloud-account', help='ATI cloud account email; browser sign-in confirms ownership')
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.action == 'identity-access':
            if environment(root, args.requested_state) != str(Path(args.requested_state).resolve()):
                raise ValueError('Unverified environment directory')
            restore_identity_owner(args.requested_state)
        elif args.action == 'environment':
            print(environment(root, args.requested_state))
        elif args.action == 'gateway':
            print(gateway_url(root, os.environ.get('ATI_CLOUD_GATEWAY_URL', '')))
        elif args.action in {'install', 'upgrade'}:
            install(root, args.non_interactive, args.no_connect, args.provider, args.cloud_account)
        else:
            settings = saved(root / '.agent.env')
            if not settings.get('ATI_AGENT_DATA_DIR'):
                raise ValueError('Container Agent is not installed; rerun the installer and select Agent')
            docker_ready()
            if args.action == 'start':
                if not settings.get('ATI_AGENT_CLOUD_ACCOUNT'):
                    raise ValueError('Rerun Agent installation and enter the cloud account before starting this legacy installation')
                authenticate(root, settings, True)
                start(root, settings)
            elif args.action == 'stop':
                checked(root, settings, ['stop', 'agent'])
            else:
                value = container_status(root, settings)
                print('Agent container online / Agent 容器在线' if value.get('online') else 'Agent container offline / Agent 容器未在线')
                return 0 if value.get('online') else 1
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print('Agent installation: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
