"""Only synthetic bindings/model commands are used by host-side regression tests."""
import json
import asyncio
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import agent_installation as agent
from agent_container import local_agent_socket


@pytest.mark.parametrize('provider',['codex','claude'])
def test_container_install_imports_only_native_auth_and_never_runs_a_host_cli(tmp_path,monkeypatch,provider):
    monkeypatch.setenv('HOME',str(tmp_path/'home'))
    monkeypatch.setenv('ATI_AGENT_DATA_DIR',str(tmp_path/'container-data'))
    monkeypatch.setenv('ATI_ENVIRONMENT_DIR',str(tmp_path/'identity'))
    monkeypatch.setenv('ATI_AGENT_CLOUD_ACCOUNT','selected@example.test')
    state=tmp_path/'identity';state.mkdir()
    binding=b'{"installId":"synthetic-install","environmentId":"synthetic-environment","credential":"synthetic-credential"}'
    (state/'installation.json').write_bytes(binding)
    (state/'trading').mkdir();(state/'trading/runtime-grant.json').write_text('synthetic-unchanged')
    auth=Path.home()/('.codex/auth.json' if provider=='codex' else '.claude/.credentials.json')
    auth.parent.mkdir(parents=True);auth.write_text('{"synthetic":"native-auth"}')
    monkeypatch.setattr(agent,'docker_ready',lambda:None)
    monkeypatch.setattr(agent,'prepare_image',lambda root,p,prior:'synthetic-agent:'+p)
    monkeypatch.setattr(agent,'ensure_bridge_volume',lambda root: agent.bridge_volume(root))
    calls=[];imported=False
    def invoke(root,settings,args,**kwargs):
        nonlocal imported
        calls.append(args)
        assert settings['ATI_AGENT_PROVIDER']==provider
        if args[-1]=='inspect-account':
            return SimpleNamespace(returncode=0,stdout=json.dumps({'bound':True,'email':'selected@example.test'}))
        if args[-1]=='auth-status':return SimpleNamespace(returncode=0 if imported else 1,stdout='')
        if args[-1]=='import-auth':
            imported=True
            assert str(auth)+':/run/ati-native-auth:ro' in args
        output=json.dumps({'state':'online','online':True}) if args[0]=='exec' else ''
        return SimpleNamespace(returncode=0,stdout=output)
    monkeypatch.setattr(agent,'invoke',invoke)
    monkeypatch.setattr(agent,'command',lambda args,**kw:pytest.fail('Unexpected host program: '+str(args[0])))
    agent.install(tmp_path,non_interactive=True,provider=provider)
    assert any(args==['up','-d','--no-deps','--no-build','--pull','never','agent'] for args in calls)
    assert any(args[-1]=='connect' for args in calls)
    settings=agent.saved(tmp_path/'.agent.env')
    assert settings['ATI_AGENT_DATA_DIR']==str(tmp_path/'container-data')
    assert settings['ATI_AGENT_IMAGE']=='synthetic-agent:'+provider
    assert 'ATI_AGENT_HOME' not in settings
    assert (state/'installation.json').read_bytes()==binding
    assert (state/'trading/runtime-grant.json').read_text()=='synthetic-unchanged'
    assert not any((tmp_path/name).exists() for name in ('.env','.research.env','.local-tools.env'))


def test_missing_container_login_does_not_stop_previous_agent_or_silently_switch(tmp_path,monkeypatch):
    monkeypatch.setenv('ATI_AGENT_CLOUD_ACCOUNT','selected@example.test')
    monkeypatch.setenv('HOME',str(tmp_path/'empty-home'))
    monkeypatch.setenv('ATI_ENVIRONMENT_DIR',str(tmp_path/'identity'))
    monkeypatch.setenv('ATI_AGENT_DATA_DIR',str(tmp_path/'container-data'))
    monkeypatch.setattr(agent,'docker_ready',lambda:None)
    monkeypatch.setattr(agent,'prepare_image',lambda *args:'synthetic-agent:claude')
    monkeypatch.setattr(agent,'ensure_bridge_volume',lambda root: agent.bridge_volume(root))
    calls=[]
    monkeypatch.setattr(agent,'invoke',lambda root,settings,args,**kw:(calls.append(args) or SimpleNamespace(returncode=1,stdout='')))
    monkeypatch.setattr(agent,'stop_legacy_host',lambda *args:pytest.fail('old Agent must survive failed sign-in'))
    with pytest.raises(ValueError,match='sign-in required'):
        agent.install(tmp_path,non_interactive=True,provider='claude')
    assert not any(args[0] in {'stop','up'} for args in calls)
    assert not (tmp_path/'.agent.env').exists()


def test_account_prompt_requires_user_input_even_with_a_legacy_binding(tmp_path,monkeypatch):
    monkeypatch.delenv('ATI_AGENT_CLOUD_ACCOUNT',raising=False)
    monkeypatch.setattr('builtins.input',lambda prompt:'Admin@Example.test')
    assert agent.select_cloud_account({},non_interactive=False)=='admin@example.test'
    with pytest.raises(ValueError,match='cloud account email'):
        agent.select_cloud_account({},non_interactive=True)
    with pytest.raises(ValueError):
        agent.select_cloud_account({},requested='not-an-email')


def test_different_account_uses_its_own_environment_without_touching_legacy_identity(tmp_path,monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME',str(tmp_path/'data'))
    old=tmp_path/'public-environment';old.mkdir()
    binding=b'{"credential":"synthetic-old-credential","environmentId":"synthetic-demo"}'
    (old/'installation.json').write_bytes(binding)
    settings={'ATI_ENVIRONMENT_DIR':str(old),'ATI_AGENT_CLOUD_ACCOUNT':'admin@example.test',
              'ATI_CLOUD_GATEWAY_URL':'https://gateway.example.test'}
    monkeypatch.setattr(agent,'checked',lambda *args,**kwargs:SimpleNamespace(stdout=json.dumps({'bound':True,'email':'demo@example.test'})))
    selected=agent.account_environment(tmp_path,settings)
    assert selected!=str(old) and 'agent-environments' in selected
    assert not Path(selected).exists()
    assert (old/'installation.json').read_bytes()==binding
    (tmp_path/'.agent.env').write_text('ATI_AGENT_CLOUD_ACCOUNT=admin@example.test\nATI_ENVIRONMENT_DIR='+shlex.quote(selected)+'\n')
    (tmp_path/'.research.env').write_text('ATI_ENVIRONMENT_DIR='+shlex.quote(str(old))+'\n')
    assert agent.environment(tmp_path)==str(old),'separate Agent identity cannot redirect research or trading'


def test_skip_connection_keeps_existing_container_without_auth_or_lifecycle_changes(tmp_path,monkeypatch):
    (tmp_path/'.agent.env').write_text('ATI_AGENT_DATA_DIR='+shlex.quote(str(tmp_path/'saved'))+'\n')
    monkeypatch.setattr(agent,'docker_ready',lambda:pytest.fail('no existing container changes'))
    agent.install(tmp_path,no_connect=True)


def test_failed_cloud_confirmation_preserves_previous_agent_and_configuration(tmp_path,monkeypatch):
    monkeypatch.setenv('ATI_AGENT_CLOUD_ACCOUNT','selected@example.test')
    monkeypatch.setenv('ATI_AGENT_DATA_DIR',str(tmp_path/'model-data'))
    old='ATI_ENVIRONMENT_DIR='+str(tmp_path/'old-state')+'\nATI_AGENT_DATA_DIR='+str(tmp_path/'model-data')+'\n'
    (tmp_path/'.agent.env').write_text(old)
    monkeypatch.setattr(agent,'docker_ready',lambda:None)
    monkeypatch.setattr(agent,'prepare_image',lambda *args:'synthetic-image')
    monkeypatch.setattr(agent,'ensure_bridge_volume',lambda root: agent.bridge_volume(root))
    monkeypatch.setattr(agent,'stop_legacy_host',lambda *args:pytest.fail('keep the previous Agent until cloud confirmation succeeds'))
    calls=[]
    def invoke(root,settings,args,**kw):
        calls.append(args)
        return SimpleNamespace(returncode=1 if args[-1]=='connect' else 0,stdout='')
    monkeypatch.setattr(agent,'invoke',invoke)
    with pytest.raises(ValueError,match='Docker Agent operation failed'):
        agent.install(tmp_path,non_interactive=True,provider='codex')
    assert (tmp_path/'.agent.env').read_text()==old
    assert not any(args[0] in {'stop','up'} for args in calls)


def test_installed_agent_socket_checks_model_and_cancels_work_on_disconnect(monkeypatch):
    monkeypatch.setenv("ATI_AGENT_PROVIDER", "codex")

    async def scenario(path):
        started, cancelled = asyncio.Event(), asyncio.Event()
        requests = []

        async def inference(adapter, request):
            requests.append((adapter, request))
            if request["messages"][0]["content"] == "cancel":
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            return {"text": '{"status":"ok"}'}

        async def send(model, message):
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(json.dumps({"model": model, "timeout": 3, "messages": [
                {"role": "user", "content": message}]}).encode() + b"\n")
            await writer.drain()
            return reader, writer

        async with local_agent_socket(path, "test_cli", infer=inference):
            reader, writer = await send("claude", "ignore")
            assert json.loads(await reader.readline()) == {"error": "LOCAL_AGENT_MODEL_MISMATCH"}
            writer.close()
            await writer.wait_closed()
            assert not requests
            _, writer = await send("codex", "cancel")
            await asyncio.wait_for(started.wait(), 1)
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(cancelled.wait(), 1)
            for _ in range(5):
                await asyncio.sleep(0)
            reader, writer = await send("codex", "test")
            assert json.loads(await reader.readline()) == {"text": '{"status":"ok"}'}
            writer.close()
            await writer.wait_closed()
        assert not Path(path).exists()

    with tempfile.TemporaryDirectory(prefix="ati-test-", dir="/tmp") as work:
        asyncio.run(scenario(f"{work}/agent.sock"))


def test_build_context_has_only_reviewed_client_and_container_code(tmp_path,monkeypatch):
    for relative in ('Dockerfile.local-agent','scripts/agent_container.py','agent-runtime/manifest.json'):
        destination=tmp_path/relative;destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/relative,destination)
    wheel=Path(agent.agent_package(ROOT));shutil.copyfile(wheel,tmp_path/'agent-runtime'/wheel.name)
    (tmp_path/'.env').write_text('synthetic-private-content-must-not-be-staged')
    monkeypatch.delenv('ATI_AGENT_IMAGE',raising=False)
    monkeypatch.setattr(agent.subprocess,'run',lambda *args,**kw:SimpleNamespace(returncode=1))
    monkeypatch.setattr(agent.subprocess,'check_output',lambda *args,**kw:'aarch64\n')
    calls=[]
    def build(args,**kwargs):
        calls.append(args)
        context=Path(args[-1])
        assert {path.name for path in context.iterdir()}=={'Dockerfile','agent_container.py','client.whl','client-filename'}
        assert (context/'client.whl').read_bytes()==wheel.read_bytes()
        assert '--load' in args and args[args.index('--platform')+1]=='linux/arm64'
        assert args[args.index('--target')+1]=='codex'
    monkeypatch.setattr(agent,'command',build)
    image=agent.prepare_image(tmp_path,'codex',{})
    assert image.startswith('ati-local-agent:codex-arm64-')
    assert len(calls)==1


def test_compose_isolated_agent_has_no_host_home_socket_or_service_ports():
    import yaml
    config=yaml.safe_load((ROOT/'docker-compose.agent.yml').read_text())
    assert set(config['services'])=={'agent'}
    service=config['services']['agent']
    assert service['read_only'] is True and service['cap_drop']==['ALL']
    assert service['restart']=='unless-stopped'
    assert 'ports' not in service and 'privileged' not in service
    assert not any('docker.sock' in volume or '${HOME}' in volume for volume in service['volumes'])
    assert len(service['volumes'])==3
    assert 'agent-bridge:/run/ati-agent-bridge' in service['volumes']
    trading=yaml.safe_load((ROOT/'docker-compose.yml').read_text())
    assert [name for name, value in trading['services'].items()
            if any(isinstance(volume, str) and volume.startswith('agent-bridge:')
                   for volume in value.get('volumes', []))] == ['backend', 'strategy-spec-service']
    assert 'agent-bridge:/run/ati-agent-bridge:ro' in trading['services']['backend']['volumes']
    assert trading['services']['backend']['environment']['ATI_LOCAL_AGENT_SOCKET'] == '/run/ati-agent-bridge/agent.sock'
    assert 'agent-bridge:/run/ati-agent-bridge:ro' in trading['services']['strategy-spec-service']['volumes']
    assert config['volumes']['agent-bridge'] == trading['volumes']['agent-bridge']


def test_legacy_host_handoff_retires_only_verified_own_configuration(tmp_path,monkeypatch):
    home=tmp_path/'legacy-agent';home.mkdir()
    state=str(tmp_path/'identity')
    config={'stateDir':state,'label':'com.broyustudio.ati-agent.synthetic','manager':'process',
            'socket':'/tmp/ati-legacy-'+__import__('uuid').uuid4().hex[:16]+'.sock','controlToken':'synthetic-token'}
    path=home/'service.json';path.write_text(json.dumps(config))
    agent.stop_legacy_host({'ATI_AGENT_HOME':str(home)},state)
    assert not path.exists()
    assert json.loads((home/'service.host-disabled.json').read_text())==config
    path.write_text(json.dumps({**config,'stateDir':str(tmp_path/'another-identity')}))
    with pytest.raises(ValueError,match='Unverified legacy'):
        agent.stop_legacy_host({'ATI_AGENT_HOME':str(home)},state)
    assert path.exists()

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

@pytest.mark.parametrize('scope', ['agent', 'research,agent', 'trading,agent', 'all,agent'])
def test_agent_is_independently_planned_without_docker_or_binding(tmp_path, scope):
    result = subprocess.run(['bash', str(ROOT / 'setup_and_run.sh'), '--components', scope, '--non-interactive', '--dry-run'],
                            env={**os.environ, 'ATI_ENVIRONMENT_DIR': str(tmp_path / 'must-not-exist')}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'Selected components: ' + scope in result.stdout
    assert not (tmp_path / 'must-not-exist').exists()

@pytest.mark.parametrize('selection,expected',[('3','agent'),('2, 3','research,agent')])
def test_interactive_menu_can_select_only_agent(tmp_path,selection,expected):
    result = subprocess.run(['bash', str(ROOT / 'setup_and_run.sh'), '--dry-run'], input=selection+'\n',
                            env={**os.environ, 'ATI_ENVIRONMENT_DIR': str(tmp_path / 'identity')}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'Choose components' in result.stdout
    assert 'Selected components: '+expected in result.stdout
    assert not (tmp_path / 'identity').exists()

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
