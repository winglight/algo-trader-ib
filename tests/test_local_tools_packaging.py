import os
import json
import sys
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]

def fixture(tmp_path):
    root=tmp_path/'runtime';shutil.copytree(ROOT,root)
    bins=tmp_path/'bin';bins.mkdir();log=tmp_path/'calls'
    docker=bins/'docker';docker.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALLS"\ncase "$*" in *"config --images") echo fixture:1 ;; esac\n');docker.chmod(0o755)
    env={**os.environ,'PATH':str(bins)+os.pathsep+os.environ['PATH'],'CALLS':str(log)}
    return root,log,env

def test_offline_install_preserves_data_and_has_no_service_or_identity(tmp_path):
    root,log,env=fixture(tmp_path);data=tmp_path/'existing bars';data.mkdir()
    (root/'.local-tools.env').write_text(f"ATI_MARKET_DATA_DIR='{data}'\n")
    r=subprocess.run(['bash',str(root/'scripts/install_local_tools.sh'),'upgrade'],env=env,capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    assert str(data) in (root/'.local-tools.env').read_text()
    assert log.read_text().splitlines()==['pull ghcr.io/winglight/algo-trader/ati-local-tools:latest']
    assert not (root/'.research.env').exists()

def test_replay_mounts_only_explicit_files_no_network_or_credentials(tmp_path):
    root,log,env=fixture(tmp_path);(root/'.local-tools.env').touch()
    data=tmp_path/'data';data.mkdir();output=tmp_path/'out';output.mkdir()
    args=[]
    for name in ('workflow','execution','instrument'):
        path=tmp_path/(name+'.json');path.write_text('{}');args += ['--'+name,str(path)]
    args+=['--data-root',str(data),'--output',str(output/'result.json'),'--symbol','QQQ','--timeframe','5m','--start','2026-01-01T00:00:00Z','--end','2026-01-02T00:00:00Z','--context-start','2025-12-01T00:00:00Z']
    r=subprocess.run(['bash',str(root/'scripts/run_local_backtest.sh'),*args],env=env,capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    calls=log.read_text();assert '--network none' in calls and calls.count('readonly')==4
    assert '/var/lib/ati-environment' not in calls and '--env ' not in calls and 'docker.sock' not in calls
    assert f'src={output},dst=/output' in calls

def test_existing_writer_is_preserved_without_docker_actions(tmp_path):
    root,log,env=fixture(tmp_path);(root/'.research.env').write_text('ATI_RESEARCH_IMAGE=legacy:1\n')
    env['ATI_ENVIRONMENT_DIR']=str(tmp_path/'identity')
    cid='a'*64
    obj={'id':cid,'running':True,'state':'running',
         'labels':{'com.docker.compose.project':'ati-local-research',
                   'com.docker.compose.service':'research-worker','com.docker.compose.oneoff':'False'},
         'mounts':[{'Type':'bind','Source':env['ATI_ENVIRONMENT_DIR'],
                    'Destination':'/var/lib/ati-environment','RW':True}]}
    docker=Path(env['PATH'].split(os.pathsep)[0])/'docker'
    docker.write_text('#!'+sys.executable+'\n'+
        "import os,sys\nargs=sys.argv[1:]\n"+
        "with open(os.environ['CALLS'],'a') as f:f.write(' '.join(args)+'\\n')\n"+
        "if args[0]=='ps' and args[-1].endswith('=research-worker'):print('"+cid+"')\n"+
        "elif args[0]=='inspect':print("+repr(json.dumps(obj))+")\n")
    r=subprocess.run(['bash',str(root/'scripts/install_trading_connector.sh'),'install'],env=env,capture_output=True,text=True)
    assert r.returncode==0 and 'writer presence only' in r.stdout, r.stderr
    calls=log.read_text()
    assert 'label=com.docker.compose.service=research-worker' in calls
    assert ' up ' not in calls and ' run ' not in calls and 'stop' not in calls
    assert not (tmp_path/'identity').exists()
    assert (root/'.research.env').read_text()=='ATI_RESEARCH_IMAGE=legacy:1\n'

def test_public_image_contexts_exclude_cloud():
    for name in ('local-tools','trading-connector'):
        text=(ROOT/f'Dockerfile.{name}').read_text()
        assert 'backend' not in text and 'strategy_studio' not in text
    assert 'ati_shared_sdk' in (ROOT/'Dockerfile.local-tools').read_text()
    assert 'ati_worker_orchestrator' in (ROOT/'Dockerfile.trading-connector').read_text()

def test_connector_status_uses_saved_config_without_evaluating_shell(tmp_path):
    root,log,env=fixture(tmp_path);sentinel=tmp_path/'should-not-exist'
    (root/'.trading-connector.env').write_text(f"ATI_ENVIRONMENT_DIR='{tmp_path}/identity'\nUNRECOGNIZED=$(touch {sentinel})\n")
    r=subprocess.run(['bash',str(root/'scripts/install_trading_connector.sh'),'status'],env=env,capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    assert not sentinel.exists()
    assert f'--env-file {root}/.trading-connector.env' in log.read_text()
    assert log.read_text().strip().endswith(' ps')

def test_connector_invalid_saved_config_fails_before_docker(tmp_path):
    root,log,env=fixture(tmp_path)
    (root/'.trading-connector.env').write_text("ATI_ENVIRONMENT_DIR='unterminated\n")
    r=subprocess.run(['bash',str(root/'scripts/install_trading_connector.sh'),'status'],env=env,capture_output=True,text=True)
    assert r.returncode!=0 and not log.exists()

def test_replay_rejects_arbitrary_docker_flags(tmp_path):
    root,log,env=fixture(tmp_path);(root/'.local-tools.env').touch()
    r=subprocess.run(['bash',str(root/'scripts/run_local_backtest.sh'),'--privileged'],env=env,capture_output=True,text=True)
    assert r.returncode==2 and not log.exists()

def test_offline_install_preserves_old_config_when_pull_fails(tmp_path):
    root,log,env=fixture(tmp_path);config=root/'.local-tools.env'
    old="ATI_MARKET_DATA_DIR='/tmp/fixture-old-data'\n";config.write_text(old)
    # Explicit data override confines this test to tmp_path even on failed pull.
    env['ATI_MARKET_DATA_DIR']=str(tmp_path/'data')
    docker=Path(env['PATH'].split(os.pathsep)[0])/'docker';docker.write_text('#!/bin/bash\nexit 7\n')
    r=subprocess.run(['bash',str(root/'scripts/install_local_tools.sh'),'upgrade'],env=env,capture_output=True,text=True)
    assert r.returncode==7 and config.read_text()==old

def test_explicit_trading_installer_enables_grant_for_existing_identity(tmp_path):
    root,log,env=fixture(tmp_path);state=tmp_path/'identity';(state/'trading').mkdir(parents=True)
    (state/'installation.json').write_text('{"credential":"fixture"}')
    (state/'trading/runtime-grant.json').write_text('{}')
    env['ATI_ENVIRONMENT_DIR']=str(state)
    r=subprocess.run(['bash',str(root/'scripts/install_trading_connector.sh'),'upgrade'],env=env,capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    assert (state/'trading-enabled').exists()
    assert 'up -d --no-deps trading-connector' in log.read_text()
    assert 'RESEARCH' not in log.read_text()

def test_trade_success_instructions_are_preserved():
    text=(ROOT/'setup_and_run.sh').read_text()
    assert 'if wait_for_http "$APP_URL" 90; then open_browser; fi' in text
    assert 'password: see ADMIN_PASSWORD' in text
    assert 'echo "  password: ${' not in text

def test_installer_update_scope_does_not_infer_installation_from_template(tmp_path):
    script=(ROOT/'scripts/install.sh').read_text()
    start=script.index('resolve_update_components() {')
    end=script.index('\nhas_cmd()',start)
    function=script[start:end]
    (tmp_path/'docker-compose.yml').touch()
    r=subprocess.run(['bash','-c',function+'\nresolve_update_components'],env={**os.environ,'INSTALL_DIR':str(tmp_path),'UPDATE_MODE':'1','REQUESTED_COMPONENTS':''},capture_output=True,text=True)
    assert r.returncode!=0 and 'unknown' in r.stderr and 'No service was stopped' in r.stderr
    (tmp_path/'.research.env').touch()
    for trading,expected in [(False,'research'),(True,'all')]:
        if trading:(tmp_path/'.env').touch()
        r=subprocess.run(['bash','-c',function+'\nresolve_update_components; echo "$UPDATE_COMPONENTS"'],env={**os.environ,'INSTALL_DIR':str(tmp_path),'UPDATE_MODE':'1','REQUESTED_COMPONENTS':''},capture_output=True,text=True)
        assert r.returncode==0 and r.stdout.strip()==expected
