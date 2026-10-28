from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]


def fixture(tmp_path):
    root=tmp_path/'runtime';shutil.copytree(ROOT,root)
    state=tmp_path/'identity'; bins=tmp_path/'bin';bins.mkdir()
    log=tmp_path/'actions'
    docker=bins/'docker'
    docker.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$CALLS"
if [[ "$1" == compose && "$*" == *" pull "* && "${FAIL_PULL:-0}" == 1 ]]; then exit 17; fi
''');docker.chmod(0o755)
    env={**os.environ,'PATH':str(bins)+os.pathsep+os.environ['PATH'],
         'ATI_ENVIRONMENT_DIR':str(state),'CALLS':str(log)}
    return root,state,log,env


def test_research_env_is_data_and_image_tag_preserved(tmp_path):
    root,state,log,env=fixture(tmp_path);sentinel=tmp_path/'owned'
    (root/'.research.env').write_text(f"ATI_ENVIRONMENT_DIR='{state}'\nIGNORED=$(touch {sentinel})\nATI_RESEARCH_IMAGE='old:1'\n")
    # Only replace our own handoff boundary; shell still performs actual lock,
    # safe parsing, pull and private atomic env persistence.
    (root/'scripts/connector_writer_handoff.py').write_text('import sys;raise SystemExit(0)\n')
    env['ATI_UPDATE_IMAGE_TAG']='0.1.70'
    r=subprocess.run(['bash',str(root/'scripts/install_research.sh'),'upgrade'],env=env,capture_output=True,text=True,timeout=15)
    assert r.returncode==0,r.stderr
    assert not sentinel.exists()
    saved=(root/'.research.env').read_text()
    assert 'ati-local-research:0.1.70' in saved and 'old:1' not in saved
    assert (root/'.research.env').stat().st_mode & 0o777==0o600
    calls=log.read_text()
    assert '--project-name ati-local-research' in calls
    assert '--env-file /dev/null' in calls
    assert 'exec' not in calls and 'backend' not in calls
    assert not (state/'trading-enabled').exists()


def test_research_status_has_no_filesystem_mutation(tmp_path):
    root,state,log,env=fixture(tmp_path)
    r=subprocess.run(['bash',str(root/'scripts/install_research.sh'),'status'],env=env,capture_output=True,text=True)
    assert r.returncode==0 and not state.exists()
    assert not (root/'.research.env').exists()
    assert log.read_text().rstrip().endswith(' ps')


def test_failure_does_not_write_success_or_start_connector(tmp_path):
    root,state,log,env=fixture(tmp_path);env['FAIL_PULL']='1'
    r=subprocess.run(['bash',str(root/'scripts/install_research.sh'),'install'],env=env,capture_output=True,text=True)
    assert r.returncode!=0
    assert not (root/'.research.env').exists()
    assert not (state/'trading-enabled').exists()
    assert ' up ' not in log.read_text() and ' run ' not in log.read_text()
    assert 'installed' not in r.stdout.lower()


def test_offline_install_record_survives_binding_failure(tmp_path):
    root,state,log,env=fixture(tmp_path)
    (root/'scripts/install_local_tools.sh').write_text('#!/bin/bash\ntouch "$ROOT_FIXTURE/offline-installed"\n')
    (root/'scripts/install_research.sh').write_text('#!/bin/bash\nexit 3\n')
    env['ROOT_FIXTURE']=str(root)
    r=subprocess.run(['bash',str(root/'setup_and_run.sh'),'--components','research','--non-interactive'],env=env,capture_output=True,text=True,timeout=15)
    assert r.returncode==3
    assert (root/'offline-installed').exists()
    assert (root/'.components.env').read_text()=='ATI_INSTALLED_COMPONENTS=research\n'
    assert 'Done.' not in r.stdout and not (state/'trading-enabled').exists()


@pytest.mark.parametrize('value',['ATI_ENVIRONMENT_DIR=one two\n','ATI_ENVIRONMENT_DIR=one\nATI_ENVIRONMENT_DIR=two\n'])
def test_malformed_env_stops_before_docker(tmp_path,value):
    root,state,log,env=fixture(tmp_path);(root/'.research.env').write_text(value)
    r=subprocess.run(['bash',str(root/'scripts/install_research.sh'),'install'],env=env,capture_output=True,text=True)
    assert r.returncode!=0 and not log.exists() and not state.exists()


# Permanent shell regressions from the independent writer-isolation review.
def run_with_known_writer(tmp_path, service, old_marker):
 root=tmp_path/'runtime';shutil.copytree(ROOT,root)
 identity=tmp_path/'identity';(identity/'trading').mkdir(parents=True)
 (identity/'installation.json').write_text(json.dumps({'credential':'fixture'}));(identity/'trading/runtime-grant.json').write_text('{}')
 if old_marker:(root/'.research.env').write_text('ATI_ENVIRONMENT_DIR='+str(identity)+'\n')
 bins=tmp_path/'bin';bins.mkdir();log=tmp_path/'commands';cid='a'*64
 obj={'id':cid,'running':True,'state':'running','labels':{'com.docker.compose.project':'ati-local-research' if service=='research-worker' else 'ati-trading-connector','com.docker.compose.service':service,'com.docker.compose.oneoff':'False'},'mounts':[{'Type':'bind','Source':str(identity),'Destination':'/var/lib/ati-environment','RW':True}]}
 docker=bins/'docker';docker.write_text('#!'+__import__('sys').executable+'\n'+'''import sys,os,json
args=sys.argv[1:]
with open(os.environ['CALLS'],'a') as f:f.write(' '.join(args)+'\\n')
obj=json.loads(os.environ['WRITER'])
if args[0]=='ps':
 if args[-1].endswith('='+obj['labels']['com.docker.compose.service']):print(obj['id'])
elif args[0]=='inspect':print(json.dumps(obj))
elif args[0]=='compose' and 'ps' in args:print(obj['id'])
''');docker.chmod(0o755)
 env={**os.environ,'PATH':str(bins)+os.pathsep+os.environ['PATH'],'ATI_ENVIRONMENT_DIR':str(identity),'CALLS':str(log),'WRITER':json.dumps(obj)}
 r=subprocess.run(['bash',str(root/'scripts/install_trading_connector.sh'),'upgrade'],env=env,text=True,capture_output=True,timeout=10)
 return r,log.read_text(),identity

def test_known_research_is_retained_without_marker(tmp_path):
 r,calls,identity=run_with_known_writer(tmp_path,'research-worker',False)
 assert r.returncode==0,r.stderr
 assert ' up -d ' not in calls
 assert not (identity/'trading-enabled').exists()

def test_prior_trading_restored_after_failed_research_can_retry(tmp_path):
 r,calls,identity=run_with_known_writer(tmp_path,'trading-connector',True)
 assert r.returncode==0,r.stderr
 assert ' up -d --no-deps trading-connector' in calls
 assert ' connect ' not in calls

