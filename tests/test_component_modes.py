from pathlib import Path
import os
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]

def sh(script, env=None):
    return subprocess.run(['bash', '-c', script], text=True, capture_output=True, env={**os.environ, **(env or {})}, timeout=15)

@pytest.mark.parametrize('previous,requested,expected', [(None,None,'all'),('research',None,'research'),('trading',None,'trading'),('all',None,'all'),('all','research','research'),('research','trading','trading')])
def test_plan_before_any_configuration_or_docker(tmp_path, previous, requested, expected):
    root=tmp_path/'runtime'; shutil.copytree(ROOT,root)
    state=tmp_path/'state'
    if previous: (root/'.components.env').write_text(f'ATI_INSTALLED_COMPONENTS={previous}\n')
    args=['bash',str(root/'setup_and_run.sh'),'--non-interactive','--dry-run']
    if requested: args += ['--components', requested]
    result=subprocess.run(args,text=True,capture_output=True,env={**os.environ,'ATI_ENVIRONMENT_DIR':str(state)},timeout=15)
    assert result.returncode == 0, result.stderr
    assert f'Selected components: {expected}' in result.stdout
    assert not state.exists()
    assert not (root/'.env').exists()
    assert not (root/'.research.env').exists()

@pytest.mark.parametrize('recorded', ['research','trading','all'])
def test_metadata_and_union_preserve_existing_components(tmp_path, recorded):
    result=sh(f'source "{ROOT}/scripts/component_modes.sh"; ati_record_component "$TARGET" research "$OLD"; ati_detect_components "$TARGET"',{'TARGET':str(tmp_path),'OLD':recorded})
    assert result.returncode==0, result.stderr
    assert result.stdout.strip()==('research' if recorded=='research' else 'all')
    assert (tmp_path/'.components.env').stat().st_mode & 0o777 == 0o600

@pytest.mark.parametrize('files,expected',[(['.env'],'trading'),(['.research.env'],'research'),(['.env','.research.env'],'all'),([], '')])
def test_legacy_detection(tmp_path, files, expected):
    for name in files: (tmp_path/name).touch()
    result=sh(f'source "{ROOT}/scripts/component_modes.sh"; ati_detect_components "$TARGET"',{'TARGET':str(tmp_path)})
    assert result.returncode==0 and result.stdout.strip()==expected

def test_metadata_is_not_executed(tmp_path):
    sentinel=tmp_path/'owned'; (tmp_path/'.components.env').write_text(f'ATI_INSTALLED_COMPONENTS=$(touch {sentinel})\n')
    result=sh(f'source "{ROOT}/scripts/component_modes.sh"; ati_detect_components "$TARGET"',{'TARGET':str(tmp_path)})
    assert result.returncode!=0 and not sentinel.exists()

def test_research_dispatch_does_not_enter_trading_setup(tmp_path):
    root=tmp_path/'runtime'; shutil.copytree(ROOT,root); state=tmp_path/'state'
    commands=tmp_path/'bin'; commands.mkdir(); docker=commands/'docker'
    docker.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALLS"\n'); docker.chmod(0o755)
    (root/'scripts/install_local_tools.sh').write_text('#!/bin/bash\nprintf "research %s \\n" "$*" >> "$CALLS"\n')
    log=tmp_path/'calls'
    result=subprocess.run(['bash',str(root/'setup_and_run.sh'),'--components','research','--non-interactive'],text=True,capture_output=True,env={**os.environ,'PATH':str(commands)+os.pathsep+os.environ['PATH'],'ATI_ENVIRONMENT_DIR':str(state),'CALLS':str(log)},timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    calls=log.read_text(); assert 'research install' in calls
    assert 'compose version' in calls and 'info' in calls
    assert 'pull' not in calls and 'down' not in calls and 'exec' not in calls
    assert not (root/'.env').exists() and not (state/'trading-enabled').exists()
    assert (root/'.components.env').read_text()=='ATI_INSTALLED_COMPONENTS=research\n'

def test_invalid_or_missing_mode_is_side_effect_free(tmp_path):
    for tail in [['--components','wrong'],['--components']]:
        result=subprocess.run(['bash',str(ROOT/'setup_and_run.sh'),'--non-interactive',*tail],env={**os.environ,'ATI_ENVIRONMENT_DIR':str(tmp_path/'state')},capture_output=True,text=True)
        assert result.returncode==2 and not (tmp_path/'state').exists()

def test_local_tools_has_no_claim_or_identity():
    source=(ROOT/'docker-compose.local-tools.yml').read_text()
    assert 'local_cli' in source and 'profiles: [tools]' in source
    assert 'research-worker' not in source and 'ati-environment' not in source
    source=(ROOT/'docker-compose.trading-connector.yml').read_text()
    assert '"TRADING"' in source and 'RESEARCH' not in source

def test_download_launcher_does_not_mount_identity_or_start_services(tmp_path):
    root=tmp_path/'runtime';shutil.copytree(ROOT,root);data=root/'data/market-data';data.mkdir(parents=True)
    (root/'.local-tools.env').write_text(f"ATI_MARKET_DATA_DIR='{data}'\n")
    commands=tmp_path/'bin';commands.mkdir();log=tmp_path/'calls';docker=commands/'docker'
    docker.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALLS"\ncase "$*" in *"config --images") echo ghcr.io/winglight/algo-trader/ati-local-tools:fixture ;; esac\n');docker.chmod(0o755)
    result=subprocess.run(['bash',str(root/'scripts/download_market_data.sh'),'--provider','alpaca','--symbols','SPY','--timeframes','1m'],capture_output=True,text=True,env={**os.environ,'PATH':str(commands)+os.pathsep+os.environ['PATH'],'CALLS':str(log),'APCA_API_KEY_ID':'fixture-secret-never-log'},timeout=15)
    assert result.returncode==0,result.stderr
    calls=log.read_text(); assert 'run --rm --user' in calls and '--cap-drop ALL' in calls
    assert '--env APCA_API_KEY_ID' in calls and 'fixture-secret-never-log' not in calls
    assert ':/app/data/market-data:rw' in calls
    assert 'ati_shared_sdk.common.market_data.local_download' in calls
    assert 'runtime-grant' not in calls and '/var/lib/ati-environment' not in calls
    assert ' up ' not in calls and ' restart ' not in calls and '--execute' not in calls
    assert '--ib-host host.docker.internal' in calls
