"""Resource-bounded Compose executor pool. This module never holds credentials."""
from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
import time
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path

POOL_VOLUME = 'ati-local-research-pool'
POOL_SERVICE = 'research-executor'
KEYS = (
    'ATI_RESEARCH_CPUS', 'ATI_RESEARCH_MEMORY', 'ATI_RESEARCH_CPU_OVERCOMMIT',
    'ATI_RESEARCH_EXECUTOR_CPUS', 'ATI_RESEARCH_EXECUTOR_MEMORY',
    'ATI_RESEARCH_COORDINATOR_CPUS', 'ATI_RESEARCH_COORDINATOR_MEMORY',
    'ATI_RESEARCH_EXECUTOR_TMPFS_MB', 'ATI_RESEARCH_MAX_PARALLEL',
    'ATI_RESEARCH_POOL_SIZE', 'ATI_RESEARCH_POOL_CPU_BUDGET',
    'ATI_RESEARCH_POOL_MEMORY_BYTES', 'ATI_RESEARCH_EXECUTION_MODE',
)


class PoolError(ValueError):
    pass


def positive(value, name):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0:
            raise InvalidOperation
        return number
    except (InvalidOperation, ValueError, TypeError):
        raise PoolError(f'{name} must be a finite positive number') from None


def memory_bytes(value):
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?)(?:i?b)?', str(value).strip().lower())
    if not match:
        raise PoolError('memory budget must use bytes or k/m/g/t units')
    size = positive(match[1], 'memory') * (1024 ** ('kmgt'.index(match[2]) + 1) if match[2] else 1)
    return int(size)


def decimal_text(value):
    return format(value.normalize(), 'f')


def plan(capacity, host, settings):
    """Total budgets include the coordinator; memory may never be oversold."""
    try:
        member, protocol = capacity['membershipMaxParallel'], capacity['protocolMaxParallel']
        if isinstance(member, bool) or isinstance(protocol, bool) or not isinstance(member, int) or not isinstance(protocol, int):
            raise ValueError
        if member < 0 or not 1 <= protocol <= 32:
            raise ValueError
        host_cpus = positive(host['NCPU'], 'Docker CPU count')
        host_memory = int(host['MemTotal'])
        if host_memory <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise PoolError('valid server capacity and Docker resource information are required') from None
    if member == 0:
        raise PoolError('membership currently allows no research execution slots')
    overcommit = positive(settings.get('ATI_RESEARCH_CPU_OVERCOMMIT', '1'), 'CPU overcommit')
    if overcommit < 1 or overcommit > 8:
        raise PoolError('CPU overcommit must be between 1 and 8; memory cannot be overcommitted')
    cpu_request = settings.get('ATI_RESEARCH_CPUS', 'auto')
    cpu_budget = host_cpus * overcommit
    if cpu_request != 'auto':
        cpu_budget = min(cpu_budget, positive(cpu_request, 'total CPU budget'))
    # Keep host/Docker headroom; never multiply the old 4 GiB total by replicas.
    memory_request = settings.get('ATI_RESEARCH_MEMORY', 'auto')
    memory_budget = max(0, host_memory - 256 * 1024**2)
    if memory_request == 'auto':
        memory_budget = min(memory_budget, max(0, host_memory - max(512 * 1024**2, host_memory // 10)))
    else:
        memory_budget = min(memory_budget, memory_bytes(memory_request))
    executor_cpu = positive(settings.get('ATI_RESEARCH_EXECUTOR_CPUS', '.5'), 'executor CPU')
    coordinator_cpu = positive(settings.get('ATI_RESEARCH_COORDINATOR_CPUS', '.25'), 'coordinator CPU')
    executor_memory = memory_bytes(settings.get('ATI_RESEARCH_EXECUTOR_MEMORY', '1536m'))
    coordinator_memory = memory_bytes(settings.get('ATI_RESEARCH_COORDINATOR_MEMORY', '256m'))
    if executor_memory < 512 * 1024**2 or coordinator_memory < 128 * 1024**2:
        raise PoolError('executor needs at least 512 MiB and coordinator at least 128 MiB')
    tmpfs = positive(settings.get('ATI_RESEARCH_EXECUTOR_TMPFS_MB', '512'), 'executor tmpfs MiB')
    if tmpfs != int(tmpfs) or tmpfs * 1024**2 >= executor_memory:
        raise PoolError('executor tmpfs must be whole MiB below its memory limit')
    cpu_slots = max(0, int(((cpu_budget - coordinator_cpu) / executor_cpu).to_integral_value(rounding=ROUND_FLOOR)))
    memory_slots = max(0, (memory_budget - coordinator_memory) // executor_memory)
    limit = min(member, protocol)
    requested = settings.get('ATI_RESEARCH_MAX_PARALLEL', 'auto')
    if requested != 'auto':
        cap = positive(requested, 'maximum parallel tasks')
        if cap != int(cap):
            raise PoolError('maximum parallel tasks must be an integer or auto')
        limit = min(limit, int(cap))
    size = min(limit, cpu_slots, memory_slots)
    if size < 1:
        raise PoolError('total CPU/memory budget cannot fit one executor plus its coordinator')
    values = {
        'ATI_RESEARCH_CPUS': cpu_request,
        'ATI_RESEARCH_MEMORY': memory_request,
        'ATI_RESEARCH_CPU_OVERCOMMIT': decimal_text(overcommit),
        'ATI_RESEARCH_EXECUTOR_CPUS': decimal_text(executor_cpu),
        'ATI_RESEARCH_COORDINATOR_CPUS': decimal_text(coordinator_cpu),
        'ATI_RESEARCH_EXECUTOR_MEMORY': str(executor_memory),
        'ATI_RESEARCH_COORDINATOR_MEMORY': str(coordinator_memory),
        'ATI_RESEARCH_EXECUTOR_TMPFS_MB': str(int(tmpfs)),
        'ATI_RESEARCH_MAX_PARALLEL': requested,
        'ATI_RESEARCH_POOL_SIZE': str(size),
        'ATI_RESEARCH_POOL_CPU_BUDGET': decimal_text(cpu_budget),
        'ATI_RESEARCH_POOL_MEMORY_BYTES': str(memory_budget),
        'ATI_RESEARCH_EXECUTION_MODE': 'pool',
    }
    return values, {'membership': member, 'cpuSlots': cpu_slots, 'memorySlots': memory_slots,
                    'executors': size, 'coordinators': 1, 'overcommit': decimal_text(overcommit)}


def save(root, values):
    path = root / '.research.env'
    fields = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        key, sep, value = line.partition('=')
        if sep:
            parts = shlex.split(value)
            if len(parts) != 1 or key in fields:
                raise PoolError('invalid saved research configuration')
            fields[key] = parts[0]
    fields.update(values)
    fd, name = tempfile.mkstemp(prefix='.research.env.', dir=root)
    try:
        with os.fdopen(fd, 'w') as file:
            file.write(''.join(f'{key}={shlex.quote(value)}\n' for key, value in fields.items()))
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    os.environ.update(values)


def prepare(root, directory, command, compose):
    # capacity is a read-only action and must not connect, heartbeat, or claim.
    try:
        capacity = json.loads(command(compose(root) + ['run', '--rm', '--no-deps', '-T',
            '--volume', f'{directory}:/var/lib/ati-environment:ro', 'research-worker', 'capacity',
            '--gateway', os.environ.get('ATI_CLOUD_GATEWAY_URL', 'https://ati.broyustudio.com'),
            '--state-dir', '/var/lib/ati-environment']))
        host = json.loads(command(['docker', 'info', '--format',
            '{"NCPU":{{json .NCPU}},"MemTotal":{{json .MemTotal}}}']))
    except (TypeError, ValueError):
        raise PoolError('capacity query failed; check Cloud connection/authorization and compatible Cloud/image versions before starting a pool') from None
    values, summary = plan(capacity, host, os.environ)
    save(root, values)
    print(f"Research pool: {summary['executors']} executor container(s) + 1 coordinator; "
          f"membership {summary['membership']}, CPU capacity {summary['cpuSlots']}, "
          f"memory capacity {summary['memorySlots']}, CPU overcommit {summary['overcommit']}. "
          'Memory limits are per-container and their sum stays within the total budget.')
    return summary['executors']


def discover(command, inspect, directory):
    ids = command(['docker', 'ps', '--all', '--quiet', '--filter',
        'label=com.docker.compose.project=ati-local-research', '--filter',
        'label=com.docker.compose.service=research-executor']).split()
    if len(ids) != len(set(ids)):
        raise PoolError('duplicate executor discovery response')
    found = []
    for cid in ids:
        item = inspect(cid)
        labels = item['labels']
        if (labels.get('com.docker.compose.project') != 'ati-local-research'
                or labels.get('com.docker.compose.service') != POOL_SERVICE
                or str(labels.get('com.docker.compose.oneoff', '')).lower() != 'false'):
            raise PoolError('unverified executor labels; no container was changed')
        identity = labels.get('io.ati.research.identity-directory')
        if not isinstance(identity, str) or Path(identity).expanduser().resolve() != directory.resolve():
            raise PoolError('executor pool belongs to another installation; no container was changed')
        mounts = item['mounts']
        pool = [m for m in mounts if m.get('Destination') == '/var/lib/ati-pool']
        if (len(pool) != 1 or pool[0].get('Type') != 'volume'
                or pool[0].get('Name') != POOL_VOLUME or pool[0].get('RW') is not True):
            raise PoolError('unverified executor pool mount; no container was changed')
        for mount in mounts:
            target = mount.get('Destination', '')
            if target == '/app/data/market-data':
                if mount.get('RW') is not False:
                    raise PoolError('executor market data must be read-only')
            elif target not in {'/var/lib/ati-pool', '/tmp', '/var/lib/ati-tasks'}:
                raise PoolError('executor has an unexpected mount; no container was changed')
        if item.get('state') not in ('running', 'created', 'exited') or item['running'] != (item['state'] == 'running'):
            raise PoolError('executor state is transitional or unknown')
        found.append(item)
    return found


def verify(command, inspect, directory, expected):
    items = discover(command, inspect, directory)
    if len(items) != expected or any(not item['running'] for item in items):
        raise PoolError('executor replica count or running state does not match the configured pool')
    # A second observation catches immediate boot failures without claiming work.
    time.sleep(1)
    again = discover(command, inspect, directory)
    if {item['id'] for item in again if item['running']} != {item['id'] for item in items}:
        raise PoolError('executor replica state changed during startup verification')
