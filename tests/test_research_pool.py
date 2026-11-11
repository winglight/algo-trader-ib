import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import research_pool_installation as pool

HOST = {'NCPU': 8, 'MemTotal': 16 * 1024**3}
CAPACITY = {'membershipMaxParallel': 8, 'protocolMaxParallel': 32}


def test_old_budget_is_total_and_still_fits_two_containers():
    values, result = pool.plan(CAPACITY, HOST, {'ATI_RESEARCH_CPUS': '2', 'ATI_RESEARCH_MEMORY': '4g'})
    assert result['executors'] == 2 and result['coordinators'] == 1
    assert int(values['ATI_RESEARCH_EXECUTOR_MEMORY']) * 2 + int(values['ATI_RESEARCH_COORDINATOR_MEMORY']) <= 4 * 1024**3
    assert values['ATI_RESEARCH_MEMORY'] == '4g'


def test_auto_uses_available_resources_up_to_membership():
    values, result = pool.plan(CAPACITY, HOST, {})
    assert result['executors'] == 8
    assert values['ATI_RESEARCH_CPUS'] == values['ATI_RESEARCH_MEMORY'] == 'auto'
    _, result = pool.plan({'membershipMaxParallel': 32, 'protocolMaxParallel': 32}, {'NCPU': 2, 'MemTotal': 4 * 1024**3}, {})
    assert result['executors'] == 2


def test_cpu_overcommit_is_explicit_and_memory_remains_hard():
    host = {'NCPU': 2, 'MemTotal': 16 * 1024**3}
    _, baseline = pool.plan(CAPACITY, host, {})
    values, expanded = pool.plan(CAPACITY, host, {'ATI_RESEARCH_CPU_OVERCOMMIT': '2'})
    assert baseline['executors'] == 3 and expanded['executors'] == 7
    assert values['ATI_RESEARCH_POOL_CPU_BUDGET'] == '4'
    _, bounded = pool.plan(CAPACITY, host, {'ATI_RESEARCH_CPU_OVERCOMMIT': '2', 'ATI_RESEARCH_MEMORY': '4g'})
    assert bounded['executors'] == 2


@pytest.mark.parametrize('settings', [
    {'ATI_RESEARCH_CPUS': '0'}, {'ATI_RESEARCH_CPUS': 'nan'},
    {'ATI_RESEARCH_MEMORY': 'infinite'}, {'ATI_RESEARCH_MEMORY': '512m'},
    {'ATI_RESEARCH_EXECUTOR_CPUS': '-1'}, {'ATI_RESEARCH_CPU_OVERCOMMIT': '0.5'},
    {'ATI_RESEARCH_CPU_OVERCOMMIT': '9'}, {'ATI_RESEARCH_EXECUTOR_TMPFS_MB': '2048'},
    {'ATI_RESEARCH_MAX_PARALLEL': '1.5'},
])
def test_invalid_or_insufficient_resources_fail_closed(settings):
    with pytest.raises(pool.PoolError):
        pool.plan(CAPACITY, HOST, settings)


@pytest.mark.parametrize('capacity', [{}, {'membershipMaxParallel': 0, 'protocolMaxParallel': 32},
    {'membershipMaxParallel': True, 'protocolMaxParallel': 32},
    {'membershipMaxParallel': 2, 'protocolMaxParallel': 100}])
def test_no_capacity_protocol_never_guesses_a_pool(capacity):
    with pytest.raises(pool.PoolError):
        pool.plan(capacity, HOST, {})


def test_manual_cap_and_protocol_bound():
    _, result = pool.plan(CAPACITY, HOST, {'ATI_RESEARCH_MAX_PARALLEL': '2'})
    assert result['executors'] == 2
    _, result = pool.plan({'membershipMaxParallel': 100, 'protocolMaxParallel': 32},
        {'NCPU': 128, 'MemTotal': 128 * 1024**3}, {})
    assert result['executors'] == 32


def test_saved_plan_is_private_and_preserves_existing_identity(tmp_path, monkeypatch):
    for key in pool.KEYS:
        monkeypatch.delenv(key, raising=False)
    identity = str(tmp_path / 'identity with spaces')
    (tmp_path / '.research.env').write_text(f'ATI_ENVIRONMENT_DIR={json.dumps(identity)}\n')
    values, _ = pool.plan(CAPACITY, HOST, {})
    pool.save(tmp_path, values)
    saved = (tmp_path / '.research.env').read_text()
    assert identity in saved and 'ATI_RESEARCH_POOL_SIZE=8' in saved
    assert (tmp_path / '.research.env').stat().st_mode & 0o777 == 0o600


def test_compose_executor_cannot_access_credentials_or_network():
    text = (ROOT / 'docker-compose.research.yml').read_text()
    executor = text.split('  research-executor:', 1)[1].split('\nvolumes:', 1)[0]
    assert 'network_mode: none' in executor
    assert '/var/lib/ati-environment' not in executor
    assert 'docker.sock' not in text
    assert '/app/data/market-data:ro' in executor
    assert 'container_name:' not in executor
    assert 'local_pool_executor' in executor and 'read_only: true' in executor
