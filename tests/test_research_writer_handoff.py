from pathlib import Path
import importlib.util
import json
import os
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import connector_writer_handoff as handoff
import connector_installer_lock as lock


class Docker:
    def __init__(self, directory):
        self.directory = directory
        self.items = {}
        self.calls = []
        self.fail = None
        self.fail_after = False

    def writer(self, char='a', service='trading-connector', running=True, source=None, project=None):
        cid = char * 64
        self.items[cid] = {'id': cid, 'running': running, 'state': 'running' if running else 'exited',
            'labels': {'com.docker.compose.project': project or handoff.SERVICES[service],
                       'com.docker.compose.service': service, 'com.docker.compose.oneoff': 'False'},
            'mounts': [{'Type': 'bind', 'Source': str(source or self.directory),
                        'Destination': '/var/lib/ati-environment', 'RW': True}]}
        return cid

    def __call__(self, args, *, capture=True):
        self.calls.append(args)
        action = args[1]
        if action == 'ps':
            service = args[-1].split('=')[-1]
            return '\n'.join(cid for cid, v in self.items.items()
                             if v['labels']['com.docker.compose.service'] == service)
        if action == 'inspect':
            assert '.Config.Env' not in args[3]
            return json.dumps(self.items[args[-1]])
        matched_failure = action == self.fail and (action != 'compose' or ('up' in args and args[-1] == 'research-worker'))
        if matched_failure and not self.fail_after:
            raise handoff.HandoffError('simulated failure')
        if action == 'info':
            return json.dumps({'NCPU': 8, 'MemTotal': 16 * 1024**3})
        if action in ('start', 'stop'):
            item = self.items[args[-1]]
            item['running'] = action == 'start'
            item['state'] = 'running' if action == 'start' else 'exited'
        elif action == 'compose':
            if 'capacity' in args:
                return json.dumps({'membershipMaxParallel': 2, 'protocolMaxParallel': 32})
            if 'connect' in args:
                (self.directory / 'installation.json').write_text('{"credential":"fixture"}')
            elif 'stop' in args and args[-1] == 'research-executor':
                for item in self.items.values():
                    if item['labels']['com.docker.compose.service'] == 'research-executor':
                        item.update(running=False, state='exited')
            elif 'up' in args and args[-1] == 'research-executor':
                count = int(next(x for x in args if x.startswith('research-executor=')).split('=')[1])
                for cid in list(self.items):
                    if self.items[cid]['labels']['com.docker.compose.service'] == 'research-executor':
                        del self.items[cid]
                for index in range(count):
                    cid = chr(ord('e') + index) * 64
                    self.items[cid] = {'id': cid, 'running': True, 'state': 'running',
                        'labels': {'com.docker.compose.project': 'ati-local-research',
                                   'com.docker.compose.service': 'research-executor',
                                   'io.ati.research.identity-directory': str(self.directory),
                                   'com.docker.compose.oneoff': 'False'},
                        'mounts': [{'Type': 'volume', 'Name': 'ati-local-research-pool',
                                    'Destination': '/var/lib/ati-pool', 'RW': True}]}
            elif 'up' in args:
                self.writer('b', 'research-worker')
        else:
            raise AssertionError(args)
        if matched_failure and self.fail_after:
            raise handoff.HandoffError('simulated ambiguous response')
        return ''


@pytest.fixture
def docker(tmp_path, monkeypatch):
    state = tmp_path / 'identity'; state.mkdir()
    d = Docker(state)
    monkeypatch.setattr(handoff, 'command', d)
    monkeypatch.setattr(handoff.pool.time, 'sleep', lambda _: None)
    for key in handoff.pool.KEYS:
        monkeypatch.delenv(key, raising=False)
    return d


def test_trading_handoff_preserves_exact_identity(docker, tmp_path):
    old = docker.writer()
    (docker.directory / 'installation.json').write_text('{"credential":"fixture"}')
    handoff.activate(tmp_path, docker.directory)
    assert not docker.items[old]['running']
    assert docker.items['b'*64]['running']
    actions = [x[1] for x in docker.calls]
    assert actions.index('stop') < actions.index('compose')
    assert not any('connect' in x for x in docker.calls)
    assert not any('backend' in x for x in docker.calls)


def test_headless_connect_does_not_import_license(docker, tmp_path):
    handoff.activate(tmp_path, docker.directory)
    assert (docker.directory/'installation.json').exists()
    assert not (docker.directory/'trading-enabled').exists()
    assert any('connect' in x and '--no-browser' in x for x in docker.calls)
    assert not any('exec' in x or 'backend' in x for x in docker.calls)


@pytest.mark.parametrize('change', ['foreign', 'oneoff', 'multiple', 'badmount', 'transitional'])
def test_unverified_writer_is_never_stopped(docker, tmp_path, change):
    cid = docker.writer()
    if change == 'foreign': docker.items[cid]['labels']['com.docker.compose.project'] = 'old-copy'
    if change == 'oneoff': docker.items[cid]['labels']['com.docker.compose.oneoff'] = 'True'
    if change == 'multiple': docker.writer('c')
    if change == 'badmount': docker.items[cid]['mounts'][0]['RW'] = False
    if change == 'transitional': docker.items[cid]['state'] = 'restarting'
    with pytest.raises(handoff.HandoffError): handoff.activate(tmp_path, docker.directory)
    assert not any(x[1] in ('stop', 'start', 'compose') for x in docker.calls)


def test_other_identity_not_stopped(docker, tmp_path):
    other = docker.writer(source=tmp_path/'other', project='other-project')
    handoff.activate(tmp_path, docker.directory)
    assert docker.items[other]['running']
    assert not any(x[1] == 'stop' for x in docker.calls)


def test_failed_up_restores_exact_old_writer(docker, tmp_path):
    old = docker.writer()
    (docker.directory/'installation.json').write_text('{"credential":"fixture"}')
    docker.fail = 'compose'; docker.fail_after = True
    with pytest.raises(handoff.HandoffError, match='prior trading connector restored'):
        handoff.activate(tmp_path, docker.directory)
    assert docker.items[old]['running']
    assert not docker.items['b'*64]['running']
    assert ['docker', 'start', old] in docker.calls


def test_unknown_stop_never_starts_new_or_blindly_restarts(docker, tmp_path):
    docker.writer(); (docker.directory/'installation.json').write_text('{"credential":"fixture"}')
    docker.fail='stop'; docker.fail_after=True
    with pytest.raises(handoff.HandoffError): handoff.activate(tmp_path,docker.directory)
    assert not any(x[1] in ('start','compose') for x in docker.calls)


def test_existing_research_upgrade_fences_old_writer_before_pool_replacement(docker,tmp_path):
    cid=docker.writer('b','research-worker')
    (docker.directory/'installation.json').write_text('{"credential":"fixture"}')
    docker.fail='compose'
    with pytest.raises(handoff.HandoffError,match='status must be checked'):
        handoff.activate(tmp_path,docker.directory)
    assert not docker.items[cid]['running']
    assert not any(x[1]=='start' for x in docker.calls)


def test_foreign_writer_during_bind_blocks_start_and_rollback(docker,tmp_path):
    base=docker.__call__
    def changed(args,**kw):
        result=base(args,**kw)
        if 'connect' in args: docker.writer('c',project='foreign')
        return result
    handoff.command=changed
    with pytest.raises(handoff.HandoffError): handoff.activate(tmp_path,docker.directory)
    assert not any('up' in x or x[1]=='stop' for x in docker.calls)


def test_lock_exec_retains_fd_and_rejects_spoof(tmp_path):
    script = ROOT/'scripts/connector_installer_lock.py'
    check = f"import sys;sys.path.insert(0,{str(ROOT/'scripts')!r});from pathlib import Path;from connector_installer_lock import inherited;assert inherited(Path(sys.argv[1]))"
    r=subprocess.run([sys.executable,str(script),str(tmp_path),sys.executable,'-c',check,str(tmp_path)],capture_output=True,text=True)
    assert r.returncode==0,r.stderr
    r=subprocess.run([sys.executable,str(script),str(tmp_path),sys.executable,'-c','raise SystemExit(99)'],env={**os.environ,'ATI_CONNECTOR_INSTALL_LOCK_FD':'0'},capture_output=True,text=True)
    assert r.returncode==3


def test_lock_contends_and_does_not_follow_symlink(tmp_path):
    fd=lock.acquire(tmp_path)
    try:
        with pytest.raises(BlockingIOError): lock.acquire(tmp_path)
    finally: os.close(fd)
    (tmp_path/'.connector-install.lock').unlink()
    (tmp_path/'.connector-install.lock').symlink_to(tmp_path/'other')
    with pytest.raises(OSError): lock.acquire(tmp_path)
    assert not (tmp_path/'other').exists()


def test_wrong_inherited_file_rejected(tmp_path,monkeypatch):
    (tmp_path/'.connector-install.lock').touch();other=tmp_path/'other';other.touch()
    fd=os.open(other,os.O_RDWR)
    try:
        monkeypatch.setenv('ATI_CONNECTOR_INSTALL_LOCK_FD',str(fd))
        with pytest.raises(ValueError): lock.inherited(tmp_path)
    finally:os.close(fd)


def test_fixed_project_other_identity_not_replaced(docker,tmp_path):
    cid=docker.writer(source=tmp_path/'other')
    with pytest.raises(handoff.HandoffError,match='another identity'):
        handoff.activate(tmp_path,docker.directory)
    assert docker.items[cid]['running']
    assert not any(x[1] in ('stop','start','compose') for x in docker.calls)


def test_rollback_unknown_read_does_not_blind_restart(docker,tmp_path):
    docker.writer()
    (docker.directory/'installation.json').write_text('{"credential":"fixture"}')
    original=docker.__call__
    lost=False
    def request(args,**kw):
        nonlocal lost
        if lost: raise handoff.HandoffError('read failed')
        result=original(args,**kw)
        if 'up' in args:
            lost=True
            raise handoff.HandoffError('ambiguous write')
        return result
    handoff.command=request
    with pytest.raises(handoff.HandoffError,match='rollback could not be confirmed'):
        handoff.activate(tmp_path,docker.directory)
    assert not any(x[1]=='start' for x in docker.calls)


@pytest.mark.parametrize('service',['research-worker','trading-connector'])
def test_active_writer_missing_credentials_never_rebinds(docker,tmp_path,service):
    docker.writer(service=service)
    with pytest.raises(handoff.HandoffError,match='existing identity'):
        handoff.activate(tmp_path,docker.directory)
    assert not any(x[1] in ('stop','start','compose') for x in docker.calls)


def test_actual_no_writer_is_continue_not_retention(docker,tmp_path,capsys):
    assert handoff.trading_preflight(docker.directory)==0
    assert 'retained' not in capsys.readouterr().out
    assert not any(x[1] in ('start','stop','compose') for x in docker.calls)


def test_failed_research_marker_then_all_sequence_can_retry(docker,tmp_path):
    old=docker.writer()
    (docker.directory/'installation.json').write_text('{"credential":"fixture"}')
    (tmp_path/'.research.env').write_text('ATI_ENVIRONMENT_DIR='+str(docker.directory)+'\n')
    docker.fail='compose';docker.fail_after=True
    with pytest.raises(handoff.HandoffError):handoff.activate(tmp_path,docker.directory)
    assert docker.items[old]['running'] and not docker.items['b'*64]['running']
    handoff.saved_research_identity(tmp_path,docker.directory)
    assert handoff.trading_preflight(docker.directory)==0
    docker.fail=None
    handoff.activate(tmp_path,docker.directory)
    assert not docker.items[old]['running'] and docker.items['b'*64]['running']



def test_pool_is_verified_before_coordinator_starts(docker, tmp_path):
    handoff.activate(tmp_path, docker.directory)
    ups = [args for args in docker.calls if 'up' in args]
    assert ups[0][-1] == 'research-executor' and 'research-executor=2' in ups[0]
    assert ups[1][-1] == 'research-worker'
    capacity = next(args for args in docker.calls if 'capacity' in args)
    assert ':ro' in capacity[capacity.index('--volume') + 1]
    assert 'heartbeat' not in capacity and 'claim' not in capacity


def test_missing_executor_replica_never_starts_coordinator(docker, tmp_path):
    base = docker.__call__
    def incomplete(args, **kwargs):
        result = base(args, **kwargs)
        if 'up' in args and args[-1] == 'research-executor':
            docker.items.pop('f' * 64)
        return result
    handoff.command = incomplete
    with pytest.raises(handoff.HandoffError):
        handoff.activate(tmp_path, docker.directory)
    assert not any('up' in args and args[-1] == 'research-worker' for args in docker.calls)
    assert not any(item['running'] for item in docker.items.values())


def test_credential_mount_in_executor_is_rejected_before_coordinator_start(docker, tmp_path):
    base = docker.__call__
    def unsafe(args, **kwargs):
        result = base(args, **kwargs)
        if 'up' in args and args[-1] == 'research-executor':
            docker.items['e' * 64]['mounts'].append({'Type': 'bind', 'Source': str(docker.directory),
                'Destination': '/var/lib/ati-environment', 'RW': True})
        return result
    handoff.command = unsafe
    with pytest.raises(handoff.HandoffError):
        handoff.activate(tmp_path, docker.directory)
    assert not any('up' in args and args[-1] == 'research-worker' for args in docker.calls)


def test_upgrade_resizes_pool_to_changed_membership(docker, tmp_path):
    handoff.activate(tmp_path, docker.directory)
    base = docker.__call__
    def smaller(args, **kwargs):
        if 'capacity' in args:
            docker.calls.append(args)
            return json.dumps({'membershipMaxParallel': 1, 'protocolMaxParallel': 32})
        return base(args, **kwargs)
    handoff.command = smaller
    handoff.activate(tmp_path, docker.directory)
    executors = handoff.pool.discover(smaller, handoff.inspect, docker.directory)
    assert len(executors) == 1 and executors[0]['running']
    assert 'ATI_RESEARCH_POOL_SIZE=1' in (tmp_path / '.research.env').read_text()


def test_stop_fences_coordinator_and_every_executor(docker, tmp_path, monkeypatch):
    handoff.activate(tmp_path, docker.directory)
    monkeypatch.setattr(sys, 'argv', ['connector_writer_handoff.py', str(tmp_path), str(docker.directory), '--stop-research'])
    assert handoff.main() == 0
    assert not any(item['running'] for item in docker.items.values())
    assert not any('trading-connector' == args[-1] and 'stop' in args for args in docker.calls)
