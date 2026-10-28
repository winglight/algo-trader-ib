"""Install one research connector without concurrent installation-identity writers.

Only fixed compose services and their verified identity bind mounts are managed.
Never inspect container Env, modify trading services, or stop unknown writers.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from connector_installer_lock import acquire, inherited


class HandoffError(ValueError):
    pass


@dataclass(frozen=True)
class Writer:
    id: str
    project: str
    service: str
    running: bool


SERVICES = {'research-worker': 'ati-local-research', 'trading-connector': 'ati-trading-connector'}
INSPECT = ('{"id":{{json .Id}},"running":{{json .State.Running}},'
           '"state":{{json .State.Status}},"labels":{'
           '"com.docker.compose.project":{{json (index .Config.Labels "com.docker.compose.project")}},'
           '"com.docker.compose.service":{{json (index .Config.Labels "com.docker.compose.service")}},'
           '"com.docker.compose.oneoff":{{json (index .Config.Labels "com.docker.compose.oneoff")}}},'
           '"mounts":{{json .Mounts}}}')


def command(args: list[str], *, capture: bool = True) -> str:
    try:
        result = subprocess.run(args, check=True, text=True, capture_output=capture,
                                timeout=300 if capture else None)
        return result.stdout if capture else ''
    except (OSError, subprocess.SubprocessError):
        # Raw Docker errors/configs can contain private paths or credentials.
        raise HandoffError('connector command failed; inspect its status before retrying') from None


def inspect(cid: str) -> dict:
    if not re.fullmatch(r'[a-f0-9]{12,64}', cid):
        raise HandoffError('invalid container identifier')
    try:
        value = json.loads(command(['docker', 'inspect', '--format', INSPECT, cid]))
        if not isinstance(value, dict) or not isinstance(value.get('labels'), dict):
            raise ValueError()
        if not isinstance(value.get('running'), bool) or not isinstance(value.get('mounts'), list):
            raise ValueError()
        if not str(value.get('id', '')).startswith(cid):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise HandoffError('invalid connector metadata; no unknown writer will be changed') from None


def discover(directory: Path) -> list[Writer]:
    directory = directory.resolve()
    ids = set()
    # Search by both known service labels, not only our project: a legacy copy
    # may otherwise write the same identity while the new connector starts.
    for service in SERVICES:
        found = command(['docker', 'ps', '--all', '--quiet', '--filter',
                         f'label=com.docker.compose.service={service}']).split()
        if len(found) != len(set(found)):
            raise HandoffError('duplicate connector discovery response')
        ids.update(found)
    writers = []
    for cid in sorted(ids):
        item = inspect(cid)
        mounts = [m for m in item['mounts'] if isinstance(m, dict)
                  and m.get('Destination') == '/var/lib/ati-environment']
        if len(mounts) != 1 or mounts[0].get('Type') != 'bind' or mounts[0].get('RW') is not True:
            raise HandoffError('unverified connector identity mount')
        source = mounts[0].get('Source')
        if not isinstance(source, str) or not Path(source).is_absolute():
            raise HandoffError('unverified connector identity source')
        labels = item['labels']
        service = labels.get('com.docker.compose.service')
        project = labels.get('com.docker.compose.project')
        if Path(source).resolve() != directory:
            if service in SERVICES and project == SERVICES[service]:
                # Compose up would replace this fixed project despite a new env
                # path. Do not repurpose another installation's connector.
                raise HandoffError('fixed connector project belongs to another identity')
            continue
        if service not in SERVICES or project != SERVICES[service] or str(labels.get('com.docker.compose.oneoff', '')).lower() != 'false':
            raise HandoffError('another connector owns this identity; no writer was stopped')
        running = item['running']
        if item.get('state') not in ('running', 'exited', 'created') or running != (item['state'] == 'running'):
            raise HandoffError('connector state is transitional or unknown')
        writers.append(Writer(item['id'], project, service, running))
    for service in SERVICES:
        if sum(w.service == service for w in writers) > 1:
            raise HandoffError('multiple connector containers for one identity')
    if sum(w.running for w in writers) > 1:
        raise HandoffError('multiple existing writers; no automatic intervention')
    return writers


def ensure_stopped(writer: Writer, directory: Path) -> None:
    command(['docker', 'stop', writer.id])
    current = discover(directory)
    if any(w.running for w in current):
        raise HandoffError('connector stop not confirmed; new writer was not started')


def credential_present(directory: Path) -> bool:
    path = directory / 'installation.json'
    if not path.exists():
        return False
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError()
        return bool(value.get('credential'))
    except (OSError, ValueError):
        raise HandoffError('installation identity is unreadable; no automatic replacement') from None


def compose(root: Path) -> list[str]:
    return ['docker', 'compose', '--project-name', 'ati-local-research',
            '--project-directory', str(root), '--env-file', str(root / '.research.env'),
            '-f', str(root / 'docker-compose.research.yml')]


def rollback(root: Path, directory: Path, prior: Writer | None) -> None:
    # Re-read first: command failure does not prove whether creation/start ran.
    current = discover(directory)
    for writer in current:
        if writer.running and writer.service == 'research-worker':
            ensure_stopped(writer, directory)
        elif writer.running:
            raise HandoffError('rollback found an active writer; no second writer started')
    if prior is not None:
        # Only restore the exact prior trading container, never a guessed compose
        # replacement. Unknown/missing old containers leave the operation failed.
        exact = [w for w in discover(directory) if w.id == prior.id and not w.running]
        if len(exact) != 1:
            raise HandoffError('prior connector cannot be safely restored')
        command(['docker', 'start', prior.id])
        if [w.id for w in discover(directory) if w.running] != [prior.id]:
            raise HandoffError('prior connector restoration could not be confirmed')


def activate(root: Path, directory: Path) -> None:
    current = discover(directory)
    active = next((w for w in current if w.running), None)
    if active and not credential_present(directory):
        raise HandoffError('active connector has no verified credential; inspect existing identity before reconnecting')
    prior = active if active and active.service == 'trading-connector' else None
    if prior:
        # Failed or unknown stop cannot authorize connect/start or blind rollback.
        ensure_stopped(prior, directory)
    try:
        if not credential_present(directory):
            command(compose(root) + ['run', '--rm', '-T', '--user',
                    f'{os.getuid()}:{os.getgid()}', 'research-worker', 'connect', '--no-browser',
                    '--gateway', os.environ.get('ATI_CLOUD_GATEWAY_URL', 'https://ati.broyustudio.com'),
                    '--state-dir', '/var/lib/ati-environment'], capture=False)
            if not credential_present(directory):
                raise HandoffError('connection did not persist credentials')
        # Identity may have been changed by an external command while binding.
        before_start = discover(directory)
        if any(w.running and w.service != 'research-worker' for w in before_start):
            raise HandoffError('another writer appeared during connection')
        command(compose(root) + ['up', '-d', '--no-deps', 'research-worker'])
        running = [w for w in discover(directory) if w.running]
        if len(running) != 1 or running[0].service != 'research-worker':
            raise HandoffError('research connector start not confirmed')
    except HandoffError as failure:
        if active and active.service == 'research-worker':
            raise HandoffError('existing research connector upgrade failed; status must be checked before retry') from failure
        try:
            rollback(root, directory, prior)
        except HandoffError:
            raise HandoffError('research installation failed; safe rollback could not be confirmed, inspect connector status') from None
        raise HandoffError('research installation failed; no new writer remains; prior trading connector restored if present') from failure


def saved_research_identity(root: Path, directory: Path) -> None:
    path = root / '.research.env'
    if not path.exists():
        return
    found = False
    for line in path.read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep and key == 'ATI_ENVIRONMENT_DIR':
            parts = shlex.split(value)
            if len(parts) != 1 or found or Path(parts[0]).expanduser().resolve() != directory.resolve():
                raise HandoffError('saved research identity differs from selected identity')
            found = True


def trading_preflight(directory: Path) -> int:
    """Check actual writers, not just env markers in the current checkout."""
    writers = discover(directory)
    research = [w for w in writers if w.service == 'research-worker']
    if research:
        if len(research) == 1 and research[0].running:
            print('Existing running research connector retained; writer presence only, no new TRADING writer started.')
            return 10
        if not any(w.service == 'trading-connector' and w.running for w in writers):
            raise HandoffError('existing research connector is stopped; inspect before installing another writer')
        # Failed RESEARCH creation may have been stopped and exact prior TRADING
        # restored. Actual state, not the leftover marker/container, governs retry.
    active = [w for w in writers if w.running]
    if active and not credential_present(directory):
        raise HandoffError('active connector identity requires diagnosis before reconnecting')
    return 0


def main() -> int:
    try:
        root = Path(sys.argv[1]).resolve()
        directory = Path(sys.argv[2]).expanduser().resolve()
        if len(sys.argv) > 3 and sys.argv[3] == '--inspect-trading':
            saved_research_identity(root, directory)
            return trading_preflight(directory)
        fd = None if inherited(directory) else acquire(directory)
        try:
            if len(sys.argv) > 3 and sys.argv[3] == '--check-trading':
                saved_research_identity(root, directory)
                return trading_preflight(directory)
            activate(root, directory)
        finally:
            if fd is not None:
                os.close(fd)
    except HandoffError as exc:
        print(f'Research connector installation failed: {exc}.', file=sys.stderr)
        return 3
    except (ValueError, OSError, IndexError):
        print('Research connector installation did not complete. Inspect connector status before retrying; '
              'no trading services, account settings or orders were changed.', file=sys.stderr)
        return 3
    print('Research connector is running. Cloud binding/capability and runtime compatibility remain required; '
          'this is not proof of a completed research task.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
