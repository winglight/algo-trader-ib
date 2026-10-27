import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[1]
CID='a'*64

@pytest.mark.parametrize('scenario,ok',[
    ('running',True),('stopped',False),('missing',False),('multiple',False),
    ('wrongmount',False),('wrongproject',False),('wrongservice',False),
    ('oneoff',False),('unknowninspect',False),('configuredother',False),
    ('malformedshape',False),('malformedmount',False),
])
def test_readonly_retention_verifies_service_and_identity(tmp_path,scenario,ok):
    root=tmp_path/'runtime';shutil.copytree(ROOT,root)
    selected=tmp_path/'identity';sentinel=tmp_path/'injected'
    configured=selected if scenario!='configuredother' else tmp_path/'other'
    (root/'.research.env').write_text(f"ATI_ENVIRONMENT_DIR='{configured}'\nUNRELATED=$(touch {sentinel})\n")
    labels={'com.docker.compose.project':'ati-local-research','com.docker.compose.service':'research-worker','com.docker.compose.oneoff':'False'}
    if scenario=='wrongproject':labels['com.docker.compose.project']='other'
    if scenario=='wrongservice':labels['com.docker.compose.service']='other'
    if scenario=='oneoff':labels['com.docker.compose.oneoff']='True'
    obj={'id':CID,'state':'exited' if scenario=='stopped' else 'running','running':scenario!='stopped','labels':labels,'mounts':[{'Type':'bind','Destination':'/var/lib/ati-environment','Source':str(selected if scenario!='wrongmount' else tmp_path/'wrong'),'RW':True}]}
    if scenario=='malformedshape':obj=['private-unrelated-fixture']
    if scenario=='malformedmount':obj['mounts']=['private-unrelated-fixture']
    inspect=tmp_path/'inspect.json';inspect.write_text(json.dumps(obj))
    bins=tmp_path/'bin';bins.mkdir();log=tmp_path/'actions'
    docker=bins/'docker';docker.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$CALLS"
if [[ "$1" == compose ]]; then
 case "$SCENARIO" in missing) ;; multiple) printf '%s\\n%s\\n' "$CID" "$CID" ;; *) echo "$CID" ;; esac
elif [[ "$1" == inspect ]]; then
 [[ "$SCENARIO" != unknowninspect ]] || exit 1
 cat "$INSPECT"
else exit 99
fi
''');docker.chmod(0o755)
    env={**os.environ,'PATH':str(bins)+os.pathsep+os.environ['PATH'],'ATI_ENVIRONMENT_DIR':str(selected),'CALLS':str(log),'CID':CID,'SCENARIO':scenario,'INSPECT':str(inspect)}
    r=subprocess.run(['bash',str(root/'scripts/install_trading_connector.sh'),'install'],env=env,capture_output=True,text=True)
    assert (r.returncode==0)==ok,(r.stdout,r.stderr)
    assert not sentinel.exists() and not selected.exists()
    assert 'Traceback' not in r.stderr and 'private-unrelated-fixture' not in r.stderr
    calls=log.read_text() if log.exists() else ''
    assert ' up ' not in calls and ' run ' not in calls and 'stop' not in calls and 'restart' not in calls
    if ok:
        assert 'writer presence only' in r.stdout
        assert '--project-name ati-local-research' in calls
        assert '--env-file /dev/null' in calls
        assert '.Config.Env' not in calls and '{{json .Config.Labels}}' not in calls
