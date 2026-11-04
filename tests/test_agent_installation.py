"""Exercise only synthetic configuration, fake model login, and a local fixture Gateway."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import agent_installation as agent
from agent_service import adapter_module


@pytest.fixture
def packaged_runtime(tmp_path):
    target = tmp_path / 'installed-client'
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-index', '--no-deps', '--target',
                    str(target), agent.agent_package(ROOT)], check=True, capture_output=True)
    return target


def test_environment_reuses_saved_directory_without_reading_credentials(tmp_path):
    state = tmp_path / 'bound research'
    (tmp_path / '.research.env').write_text('ATI_ENVIRONMENT_DIR=' + shlex.quote(str(state)) + '\n')
    assert agent.environment(tmp_path) == str(state)
    assert not state.exists()
    assert agent.environment(tmp_path, str(state)) == str(state)
    with pytest.raises(ValueError, match='different environment'):
        agent.environment(tmp_path, str(tmp_path / 'another'))
    (tmp_path / '.agent.env').write_text('ATI_ENVIRONMENT_DIR=' + shlex.quote(str(tmp_path / 'another')))
    with pytest.raises(ValueError):
        agent.environment(tmp_path)


def test_saved_configuration_never_executes_or_defaults_over_corruption(tmp_path):
    marker = tmp_path / 'not-created'
    file = tmp_path / '.research.env'
    file.write_text('ATI_ENVIRONMENT_DIR=$(touch ' + str(marker) + ')\n')
    with pytest.raises(ValueError):
        agent.environment(tmp_path)
    assert not marker.exists()
    file.write_text('ATI_ENVIRONMENT_DIR=/one\nATI_ENVIRONMENT_DIR=/two\n')
    with pytest.raises(ValueError):
        agent.environment(tmp_path)


def fake_codex(tmp_path, monkeypatch):
    binary = tmp_path / 'codex'
    binary.write_text('#!' + sys.executable + '\nimport os,sys\nfrom pathlib import Path\np=Path(os.environ["FAKE_CODEX_LOGIN"])\nif sys.argv[1:]==["login","status"]:raise SystemExit(0 if p.exists() else 1)\nif sys.argv[1:]==["login"]:p.touch();raise SystemExit(0)\nraise SystemExit("Unexpected model call in installer fixture")\n')
    binary.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    marker = tmp_path / 'signed-in'
    monkeypatch.setenv('FAKE_CODEX_LOGIN', str(marker))
    return binary, marker


def fake_claude(tmp_path, monkeypatch):
    binary = tmp_path / 'claude'
    binary.write_text('#!' + sys.executable + '\nimport os,sys\nfrom pathlib import Path\nif sys.argv[1:]==["--help"]:print("--safe-mode --tools --strict-mcp-config --no-session-persistence --input-format --output-format");raise SystemExit(0)\np=Path(os.environ["FAKE_CLAUDE_LOGIN"])\nif sys.argv[1:]==["auth","status"]:raise SystemExit(0 if p.exists() else 1)\nif sys.argv[1:]==["auth","login"]:p.touch();raise SystemExit(0)\nraise SystemExit("Unexpected model call in installer fixture")\n')
    binary.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    marker = tmp_path / 'claude-signed-in'
    monkeypatch.setenv('FAKE_CLAUDE_LOGIN', str(marker))
    return binary, marker


def test_reuses_codex_login_and_requires_interaction_only_when_missing(tmp_path, monkeypatch):
    binary, marker = fake_codex(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='sign-in is required'):
        agent.prepare_codex(True)
    assert not marker.exists()
    assert agent.prepare_codex(False) == str(binary)
    assert marker.exists()
    assert agent.prepare_codex(True) == str(binary)


@pytest.mark.parametrize('scope', ['agent', 'research,agent', 'trading,agent', 'all,agent'])
def test_agent_is_independently_planned_without_docker_or_binding(tmp_path, scope):
    result = subprocess.run(['bash', str(ROOT / 'setup_and_run.sh'), '--components', scope, '--non-interactive', '--dry-run'],
                            env={**os.environ, 'ATI_ENVIRONMENT_DIR': str(tmp_path / 'must-not-exist')}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'Selected components: ' + scope in result.stdout
    assert not (tmp_path / 'must-not-exist').exists()


def test_agent_only_dispatch_does_not_install_docker_research_or_trading(tmp_path):
    root = tmp_path / 'installation'
    shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns('.git', '*.env', '__pycache__', '.pytest_cache', 'data', 'logs'))
    binary = tmp_path / 'bin'
    binary.mkdir()
    log = tmp_path / 'calls'
    for name in ('docker',):
        path = binary / name
        path.write_text('#!/bin/sh\necho forbidden-docker >> "$CALLS"\nexit 91\n');path.chmod(0o755)
    for name in ('install_research.sh', 'install_trading_connector.sh', 'install_local_tools.sh'):
        (root / 'scripts' / name).write_text('#!/bin/sh\necho forbidden-other-component >> "$CALLS"\nexit 92\n')
    (root / 'scripts/install_agent.sh').write_text('#!/bin/sh\necho "agent $*" >> "$CALLS"\n')
    result = subprocess.run(['bash', str(root / 'setup_and_run.sh'), '--components', 'agent', '--non-interactive'],
                            env={**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH'],
                                 'CALLS': str(log), 'ATI_ENVIRONMENT_DIR': str(tmp_path / 'identity')}, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert log.read_text().splitlines() == ['agent install --non-interactive']
    assert (root / '.components.env').read_text() == 'ATI_INSTALLED_COMPONENTS=agent\n'
    assert not (root / '.env').exists()
    assert not (tmp_path / 'identity/trading-enabled').exists()


@pytest.mark.parametrize('selection,expected',[('3','agent'),('2, 3','research,agent')])
def test_interactive_menu_can_select_only_agent(tmp_path,selection,expected):
    result = subprocess.run(['bash', str(ROOT / 'setup_and_run.sh'), '--dry-run'], input=selection+'\n',
                            env={**os.environ, 'ATI_ENVIRONMENT_DIR': str(tmp_path / 'identity')}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'Choose components' in result.stdout
    assert 'Selected components: '+expected in result.stdout
    assert not (tmp_path / 'identity').exists()


@pytest.fixture
def fixture_gateway():
    paths = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            paths.append(self.path)
            size = int(self.headers.get('Content-Length', '0'))
            payload = json.loads(self.rfile.read(size))
            assert self.headers['Authorization'] == 'Bearer synthetic-connector-credential'
            if self.path.startswith('/reject/'):
                self.send_error(403);return
            if self.path.endswith('/heartbeat'):
                assert list(payload['components']) == ['AGENT']
                body = {}
            elif self.path.endswith('/claim'):
                assert payload['component'] == 'AGENT'
                body = {'task': None}
            else:
                self.send_error(500);return
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(body).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port), paths
    finally:
        server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_installer_runs_real_agent_connector_in_background_without_trading(tmp_path, monkeypatch, fixture_gateway, packaged_runtime, provider):
    gateway, paths = fixture_gateway
    root = tmp_path / 'installation'; (root / 'scripts').mkdir(parents=True)
    shutil.copy(ROOT / 'scripts/agent_service.py', root / 'scripts/agent_service.py')
    shutil.copytree(ROOT / 'agent-runtime', root / 'agent-runtime')
    state = tmp_path / 'shared identity';state.mkdir()
    original = {'credential':'synthetic-connector-credential','installId':'synthetic-install','proof':'synthetic-proof',
                'fingerprint':'synthetic-fingerprint','environmentId':'synthetic-environment'}
    (state / 'installation.json').write_text(json.dumps(original))
    (root / '.research.env').write_text('ATI_ENVIRONMENT_DIR=' + shlex.quote(str(state)) + '\n')
    cli, marker = (fake_claude if provider == 'claude' else fake_codex)(tmp_path, monkeypatch);marker.touch()
    monkeypatch.setenv('ATI_AGENT_HOME', str(tmp_path / 'agent home'))
    monkeypatch.setenv('ATI_CLOUD_GATEWAY_URL', gateway)
    monkeypatch.delenv('ATI_ENVIRONMENT_DIR', raising=False)
    monkeypatch.setenv('ATI_AGENT_SERVICE_MODE', 'process')
    monkeypatch.setenv('ATI_AGENT_START_TIMEOUT', '8')
    monkeypatch.setenv('PYTHONPATH', str(packaged_runtime))
    monkeypatch.setattr(agent, 'prepare_python', lambda home: Path(sys.executable))
    real = agent.command
    def command(args, **kwargs):
        if list(map(str,args))[1:4] == ['-m','pip','install']:
            return
        return real(args, **kwargs)
    monkeypatch.setattr(agent,'command',command)
    config = None
    try:
        agent.install(root, non_interactive=True, provider=provider)
        config_path = tmp_path / 'agent home/service.json'
        config = json.loads(config_path.read_text())
        first = agent.control(config,'status')
        assert first['online'] is True
        assert config['provider'] == provider
        assert agent.saved(root/'.agent.env')['ATI_AGENT_PROVIDER'] == provider
        assert adapter_module(config).endswith(provider + '_agent_adapter')
        agent.start_service(config, config_path)
        assert agent.control(config,'status')['pid'] == first['pid']
        assert json.loads((state/'installation.json').read_text()) == original
        assert any(path.endswith('/heartbeat') for path in paths)
        assert not any('grant' in path or 'bind' in path or 'TRADING' in path for path in paths)
        assert not (state / 'trading').exists()
        assert config_path.stat().st_mode & 0o777 == 0o600
        assert (root/'.agent.env').stat().st_mode & 0o777 == 0o600
        if provider == 'codex':
            _, login = fake_claude(tmp_path, monkeypatch);login.touch()
            agent.install(root, non_interactive=True, provider='claude')
            config = json.loads(config_path.read_text())
            assert config['provider'] == 'claude'
            assert agent.control(config, 'status')['online']
            assert json.loads((state/'installation.json').read_text()) == original
    finally:
        if config:
            agent.stop_service(config)
    assert not agent.service_status(config).get('online')


def test_gateway_reuse_never_sends_a_saved_credential_to_another_host(tmp_path):
    (tmp_path/'.research.env').write_text('ATI_CLOUD_GATEWAY_URL=https://research.example\n')
    assert agent.gateway_url(tmp_path) == 'https://research.example'
    assert agent.gateway_url(tmp_path, 'https://research.example/') == 'https://research.example'
    with pytest.raises(ValueError, match='no credential was sent'):
        agent.gateway_url(tmp_path, 'https://another.example')
    for value in ['http://untrusted.example','https://user:secret@example.com','https://example.com?token=secret']:
        (tmp_path/'.research.env').unlink(missing_ok=True)
        with pytest.raises(ValueError):
            agent.gateway_url(tmp_path,value)


def test_no_connect_retains_an_existing_agent_without_login_or_stop(tmp_path, monkeypatch):
    home=tmp_path/'saved agent';home.mkdir()
    (home/'service.json').write_text('{"fixture":"unchanged"}')
    (tmp_path/'.agent.env').write_text('ATI_AGENT_HOME='+shlex.quote(str(home))+'\n')
    monkeypatch.setattr(agent,'prepare_codex',lambda *a,**k:pytest.fail('login must not run'))
    monkeypatch.setattr(agent,'stop_service',lambda *a:pytest.fail('existing process must not stop'))
    agent.install(tmp_path,no_connect=True)
    assert (home/'service.json').read_text()=='{"fixture":"unchanged"}'


def test_skip_connection_does_not_prompt_for_codex_sign_in(tmp_path,monkeypatch):
    binary,marker=fake_codex(tmp_path,monkeypatch)
    assert agent.prepare_codex(True,require_login=False)==str(binary)
    assert not marker.exists()


@pytest.mark.parametrize('interactive',[False,True])
def test_agent_only_bootstrap_update_preserves_other_components_without_docker(tmp_path,interactive):
    import zipfile
    installed=tmp_path/'installed';installed.mkdir()
    (installed/'.env').write_text('APP_ENV=synthetic-fixture\n')
    (installed/'setup_and_run.sh').write_text('#!/bin/sh\n')
    (installed/'.components.env').write_text('ATI_INSTALLED_COMPONENTS=all\n')
    (installed/'.agent.env').write_text('ATI_AGENT_HOME='+shlex.quote(str(tmp_path/'saved agent'))+'\n')
    (installed/'docker-compose.yml').write_text('services: {}\n')
    data=installed/'data';data.mkdir();(data/'keep').write_text('fixture-data')
    archive=tmp_path/'fixture.zip'
    with zipfile.ZipFile(archive,'w') as package:
        package.writestr('algo-trader-ib-fixture/setup_and_run.sh','#!/bin/sh\nprintf "%s\\n" "$*" > "$ARGS_FILE"\n')
    commands=tmp_path/'bin';commands.mkdir();calls=tmp_path/'docker-calls';args=tmp_path/'args'
    curl=commands/'curl';curl.write_text('#!'+sys.executable+'\nimport os,shutil,sys\nshutil.copyfile(os.environ["FIXTURE_ZIP"],sys.argv[sys.argv.index("-o")+1])\n');curl.chmod(0o755)
    docker=commands/'docker';docker.write_text('#!/bin/sh\necho forbidden >> "$DOCKER_CALLS"\nexit 99\n');docker.chmod(0o755)
    flags=[] if interactive else ['--update','--components','agent','--non-interactive']
    result=subprocess.run(['bash',str(ROOT/'scripts/install.sh'),*flags],input='\n3\ny\n' if interactive else None,
        text=True,capture_output=True,timeout=20,env={**os.environ,'PATH':str(commands)+os.pathsep+os.environ['PATH'],
        'ATI_INSTALL_DIR':str(installed),'ATI_ALLOW_UPDATE':'1','FIXTURE_ZIP':str(archive),'DOCKER_CALLS':str(calls),'ARGS_FILE':str(args)})
    assert result.returncode==0,result.stdout+result.stderr
    assert not calls.exists()
    assert '--components agent' in args.read_text()
    assert (installed/'.env').read_text()=='APP_ENV=synthetic-fixture\n'
    assert (installed/'data/keep').read_text()=='fixture-data'
    assert 'saved agent' in (installed/'.agent.env').read_text()


@pytest.mark.parametrize('platform,tool,manager',[('darwin','launchctl','launchd'),('linux','systemctl','systemd')])
def test_native_user_startup_files_preserve_quoted_paths_and_private_config(tmp_path,monkeypatch,platform,tool,manager):
    monkeypatch.setenv('HOME',str(tmp_path))
    monkeypatch.setattr(agent.sys,'platform',platform)
    monkeypatch.setattr(agent.shutil,'which',lambda name:'/fixture/'+tool if name==tool else None)
    monkeypatch.setattr(agent.subprocess,'run',lambda *a,**k:type('Result',(),{'returncode':0})())
    calls=[];monkeypatch.setattr(agent,'command',lambda args,**kw:calls.append(args))
    config={'python':'/a space/python','runner':'/a space/service.py','socket':str(tmp_path/'not-running.sock'),
      'controlToken':'synthetic-control-token','label':'com.broyustudio.synthetic-agent','path':'/a space/bin:/usr/bin',
      'log':str(tmp_path/'agent.log'),'environment':{'CODEX_HOME':'/a space/user Codex'}}
    path=tmp_path/'service.json';agent.start_service(config,path)
    assert config['manager']==manager
    assert Path(config['serviceFile']).stat().st_mode & 0o777==0o600
    assert (tmp_path/'agent.log').stat().st_mode & 0o777==0o600
    if manager=='launchd':
        import plistlib
        value=plistlib.loads(Path(config['serviceFile']).read_bytes())
        assert value['ProgramArguments']==['/a space/python','/a space/service.py',str(path)]
        assert value['EnvironmentVariables']['CODEX_HOME']=='/a space/user Codex'
    else:
        unit=Path(config['serviceFile']).read_text()
        assert '"/a space/python" "/a space/service.py"' in unit
        assert 'StandardOutput="file:' in unit
        assert any(args[-2:]==['--now','com.broyustudio.synthetic-agent.service'] for args in calls)


def test_setup_stops_before_any_install_when_saved_environment_conflicts(tmp_path):
    root=tmp_path/'installation'
    shutil.copytree(ROOT,root,ignore=shutil.ignore_patterns('.git','*.env','__pycache__','.pytest_cache','data','logs'))
    (root/'.research.env').write_text('ATI_ENVIRONMENT_DIR='+str(tmp_path/'one')+'\n')
    (root/'.agent.env').write_text('ATI_ENVIRONMENT_DIR='+str(tmp_path/'two')+'\n')
    (root/'scripts/install_agent.sh').write_text('#!/bin/sh\necho should-not-run\nexit 97\n')
    result=subprocess.run(['bash',str(root/'setup_and_run.sh'),'--components','agent','--non-interactive'],
        capture_output=True,text=True,env={k:v for k,v in os.environ.items() if k!='ATI_ENVIRONMENT_DIR'})
    assert result.returncode!=0
    assert 'different environment directories' in result.stderr
    assert 'should-not-run' not in result.stdout
    assert not (tmp_path/'one').exists()
    assert not (tmp_path/'two').exists()


def test_missing_codex_is_installed_automatically_without_model_execution(tmp_path,monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path))
    monkeypatch.setattr(agent.shutil,'which',lambda _:None)
    downloads=[]
    def download(url,env=None):
        downloads.append(url)
        binary=tmp_path/'.local/bin/codex';binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/sh\n[ "$1 $2" = "login status" ]\n');binary.chmod(0o755)
    monkeypatch.setattr(agent,'download_script',download)
    assert agent.prepare_codex(True)==str(tmp_path/'.local/bin/codex')
    assert downloads==['https://chatgpt.com/codex/install.sh']


def test_missing_python_is_prepared_automatically_with_private_uv(tmp_path,monkeypatch):
    monkeypatch.delenv('ATI_AGENT_PYTHON',raising=False)
    monkeypatch.setattr(agent.shutil,'which',lambda _:None)
    monkeypatch.setattr(agent,'python_supported',lambda p:p==tmp_path/'venv/bin/python')
    downloads=[];calls=[]
    monkeypatch.setattr(agent,'download_script',lambda url,env:downloads.append((url,env)))
    monkeypatch.setattr(agent,'command',lambda args:calls.append(args))
    assert agent.prepare_python(tmp_path)==tmp_path/'venv/bin/python'
    assert downloads[0][0]=='https://astral.sh/uv/install.sh'
    assert downloads[0][1]['UV_UNMANAGED_INSTALL']==str(tmp_path/'tools')
    assert calls==[[str(tmp_path/'tools/uv'),'venv','--python','3.11','--seed',tmp_path/'venv']]


def test_rejected_agent_stops_without_changing_shared_trading_grants(tmp_path,fixture_gateway,packaged_runtime):
    gateway,paths=fixture_gateway
    state=tmp_path/'identity';state.mkdir()
    (state/'installation.json').write_text(json.dumps({'credential':'synthetic-connector-credential',
        'installId':'synthetic-install','proof':'synthetic-proof','fingerprint':'synthetic-fingerprint',
        'environmentId':'synthetic-environment'}))
    trading=state/'trading';trading.mkdir()
    grant=trading/'runtime-grant.json';grant.write_text('{"synthetic":"unchanged"}')
    config={'python':sys.executable,'gateway':gateway+'/reject','stateDir':str(state),
        'socket':'/tmp/ati-test-'+__import__('hashlib').sha256(str(tmp_path).encode()).hexdigest()[:16]+'.sock','controlToken':'synthetic-control-token'}
    file=tmp_path/'service.json';file.write_text(json.dumps(config))
    result=subprocess.run([sys.executable,str(ROOT/'scripts/agent_service.py'),str(file)],
        capture_output=True,text=True,timeout=10,env={**os.environ,
        'PYTHONPATH':str(packaged_runtime)})
    assert result.returncode==1
    assert grant.read_text()=='{"synthetic":"unchanged"}'
    assert paths==['/reject/cloud/local-execution/heartbeat'],result.stderr
    assert not Path(config['socket']).exists()


def test_research_and_agent_selection_dispatches_both_without_trading(tmp_path):
    root=tmp_path/'installation'
    shutil.copytree(ROOT,root,ignore=shutil.ignore_patterns('.git','*.env','__pycache__','.pytest_cache','data','logs'))
    binary=tmp_path/'bin';binary.mkdir();log=tmp_path/'calls'
    docker=binary/'docker';docker.write_text('#!/bin/sh\necho "docker $*" >> "$CALLS"\n');docker.chmod(0o755)
    for filename,component in [('install_local_tools.sh','tools'),('install_research.sh','research'),('install_agent.sh','agent')]:
        (root/'scripts'/filename).write_text('#!/bin/sh\necho "'+component+' $*" >> "$CALLS"\n')
    result=subprocess.run(['bash',str(root/'setup_and_run.sh'),'--components','research,agent','--non-interactive'],
        capture_output=True,text=True,timeout=15,env={**os.environ,'PATH':str(binary)+os.pathsep+os.environ['PATH'],
        'CALLS':str(log),'ATI_ENVIRONMENT_DIR':str(tmp_path/'identity')})
    assert result.returncode==0,result.stdout+result.stderr
    assert log.read_text().splitlines()==['docker compose version','docker info','tools install','research install','agent install --non-interactive']
    assert (root/'.components.env').read_text()=='ATI_INSTALLED_COMPONENTS=research,agent\n'
    assert not (root/'.env').exists()
    assert not (tmp_path/'identity/trading-enabled').exists()


def test_docker_identity_access_never_changes_an_unrelated_directory(tmp_path,monkeypatch):
    monkeypatch.setenv('SUDO_UID',str(os.getuid()))
    monkeypatch.setenv('SUDO_GID',str(os.getgid()))
    monkeypatch.setattr(agent.os,'getuid',lambda:0)
    monkeypatch.setattr(agent.os,'fchown',lambda *args:pytest.fail('unverified directory ownership change'))
    with pytest.raises(ValueError,match='No existing connector identity'):
        agent.restore_identity_owner(tmp_path)
    (tmp_path/'installation.json').write_text('{}')
    with pytest.raises(ValueError,match='Invalid connector identity'):
        agent.restore_identity_owner(tmp_path)
    (tmp_path/'installation.json').unlink()
    (tmp_path/'installation.json').symlink_to(tmp_path/'elsewhere')
    with pytest.raises(ValueError,match='No existing connector identity'):
        agent.restore_identity_owner(tmp_path)


def test_shell_can_prepare_python_before_any_python_is_available(tmp_path):
    binary=tmp_path/'bin';binary.mkdir();log=tmp_path/'uv-calls'
    missing=binary/'python3';missing.write_text('#!/bin/sh\nexit 1\n');missing.chmod(0o755)
    uv=binary/'uv'
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$UV_CALLS"\nfor target do :; done\nmkdir -p "$target/bin"\nprintf "#!/bin/sh\\nexit 0\\n" > "$target/bin/python3"\nchmod +x "$target/bin/python3"\n');uv.chmod(0o755)
    result=subprocess.run(['bash','-c','source "$1"\nati_ensure_installer_python\ncommand -v python3','fixture',str(ROOT/'scripts/installer_python.sh')],
        capture_output=True,text=True,env={**os.environ,'PATH':str(binary)+os.pathsep+os.environ['PATH'],
        'XDG_DATA_HOME':str(tmp_path/'user data'),'UV_CALLS':str(log)})
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()==str(tmp_path/'user data/ati/installer-tools/python/bin/python3')
    assert log.read_text().startswith('venv --python 3.11 --seed ')


def test_bundled_client_has_only_reviewed_native_sources_and_verified_hashes(tmp_path):
    import hashlib
    import zipfile
    manifest=json.loads((ROOT/'agent-runtime/manifest.json').read_text())
    wheel=Path(agent.agent_package(ROOT))
    with zipfile.ZipFile(wheel) as archive:
        source_names={name for name in archive.namelist() if name.endswith('.py')}
        assert source_names=={'worker_orchestrator/__init__.py','worker_orchestrator/local_connector.py','worker_orchestrator/codex_agent_adapter.py','worker_orchestrator/claude_agent_adapter.py','worker_orchestrator/local_task_process.py'}
        for source in manifest['sources']:
            name=source['path'].removeprefix('src/')
            assert hashlib.sha256(archive.read(name)).hexdigest()==source['sha256']
        assert not any('application' in name or 'strategy_studio' in name or '.env' in name for name in archive.namelist())
    shutil.copytree(ROOT/'agent-runtime',tmp_path/'agent-runtime')
    (tmp_path/'agent-runtime'/wheel.name).write_bytes(b'corrupted synthetic wheel')
    with pytest.raises(ValueError,match='checksum mismatch'):
        agent.agent_package(tmp_path)


@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_packaged_client_executes_a_synthetic_task_through_lease_guard_and_fake_codex(tmp_path,packaged_runtime,provider):
    binary=tmp_path/'bin';binary.mkdir()
    cli=binary/provider
    if provider=='codex':
        cli.write_text('#!'+sys.executable+'\nimport sys\nfrom pathlib import Path\nassert sys.argv[1]=="exec"\nassert sys.argv[sys.argv.index("--sandbox")+1]=="read-only"\nassert "synthetic fixture prompt" in sys.stdin.read()\nPath(sys.argv[sys.argv.index("--output-last-message")+1]).write_text(\'{"fixture":"completed"}\')\n')
    else:
        cli.write_text('#!'+sys.executable+'\nimport sys,json\nassert "--safe-mode" in sys.argv and "--bare" not in sys.argv\nassert sys.argv[sys.argv.index("--tools")+1]==""\nassert "--strict-mcp-config" in sys.argv and "--no-session-persistence" in sys.argv\nmessage=json.load(sys.stdin)\nassert message["type"]=="user"\nassert "synthetic fixture prompt" in message["message"]["content"][0]["text"]\nprint(json.dumps({"type":"result","subtype":"success","is_error":False,"result":\'{"fixture":"completed"}\'}))\n')
    cli.chmod(0o755)
    script='''import asyncio,hashlib,json,sys,time
from worker_orchestrator.local_connector import Connector
async def check():
    connector=Connector('http://127.0.0.1:1',sys.argv[1],component='AGENT',agent_command=[sys.executable,'-m','worker_orchestrator.'+sys.argv[2]+'_agent_adapter'])
    packet={'prompt':'synthetic fixture prompt','variables':{}}
    task={'taskId':'synthetic-task','attempt':1,'kind':'AI','leaseExpiresAt':time.time()+30,'packet':packet,'packetHash':hashlib.sha256(json.dumps(packet,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
    try:
        result=await connector.execute(task)
        assert result['json_payload']=={'fixture':'completed'},result
        assert result['model']=='user-'+sys.argv[2],result
        print('PASS synthetic Agent task: native lease guard → adapter → fake Codex → structured result')
    finally:
        await connector.http.aclose()
asyncio.run(check())
'''
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path/'synthetic-identity'),provider],capture_output=True,text=True,timeout=15,
        env={**os.environ,'PYTHONPATH':str(packaged_runtime),'PATH':str(binary)+os.pathsep+os.environ['PATH']})
    assert result.returncode==0,result.stdout+result.stderr
    assert 'PASS synthetic Agent task' in result.stdout


def test_claude_login_is_reused_and_missing_login_is_guided(tmp_path, monkeypatch):
    binary, marker = fake_claude(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='Claude Code sign-in is required'):
        agent.prepare_claude(True)
    assert not marker.exists()
    assert agent.prepare_claude(False) == str(binary)
    assert marker.exists()
    assert agent.prepare_claude(True) == str(binary)


def test_claude_install_is_automatic_and_skip_connection_never_requests_login(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(agent.shutil, 'which', lambda _: None)
    downloads = []
    def download(url, env=None, shell=None):
        downloads.append((url, shell))
        target = tmp_path/'.local/bin';target.mkdir(parents=True)
        fake_claude(target, monkeypatch)
    monkeypatch.setattr(agent, 'download_script', download)
    assert agent.prepare_claude(True, require_login=False) == str(tmp_path/'.local/bin/claude')
    assert downloads == [('https://claude.ai/install.sh', '/bin/bash')]
    assert not (tmp_path/'.local/bin/claude-signed-in').exists()


def test_provider_selection_preserves_existing_choice_and_validates_explicit_change(monkeypatch):
    monkeypatch.delenv('ATI_AGENT_PROVIDER', raising=False)
    assert agent.select_provider({}, non_interactive=True) == 'codex'
    assert agent.select_provider({'ATI_AGENT_PROVIDER':'claude'}, non_interactive=True) == 'claude'
    monkeypatch.setattr('builtins.input', lambda _: '')
    assert agent.select_provider({'ATI_AGENT_PROVIDER':'claude'}) == 'claude'
    monkeypatch.setattr('builtins.input', lambda _: '2')
    assert agent.select_provider({}) == 'claude'
    assert agent.select_provider({}, 'codex') == 'codex'
    with pytest.raises(ValueError):
        agent.select_provider({}, 'unknown')
    with pytest.raises(ValueError):
        adapter_module({'provider':'arbitrary.module'})
    assert adapter_module({}).endswith('codex_agent_adapter')


def test_claude_cli_selection_is_planned_without_side_effects_and_rejects_unknown_provider(tmp_path):
    for provider, expected in [('claude',0),('codex',0),('unknown',2)]:
        result=subprocess.run(['bash',str(ROOT/'setup_and_run.sh'),'--components','agent','--agent-provider',provider,'--non-interactive','--dry-run'],
            capture_output=True,text=True,env={**os.environ,'ATI_ENVIRONMENT_DIR':str(tmp_path/'not-created')})
        assert result.returncode==expected,result.stdout+result.stderr
        if not expected:
            assert 'Selected Agent provider: '+provider in result.stdout
        assert not (tmp_path/'not-created').exists()
    for args in (['--agent-provider','unknown'],['--agent-provider']):
        result=subprocess.run(['bash',str(ROOT/'scripts/install.sh'),'--non-interactive',*args],
            capture_output=True,text=True,env={**os.environ,'ATI_INSTALL_DIR':str(tmp_path/'not-created')})
        assert result.returncode==2
        assert not (tmp_path/'not-created').exists()
