"""Read-only verification of an existing legacy environment writer; no health claim."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


class VerificationError(ValueError):
    pass


def verify(root: Path, selected: Path) -> None:
    values = {}
    for line in (root / '.research.env').read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep and key == 'ATI_ENVIRONMENT_DIR':
            parts = shlex.split(value)
            if len(parts) != 1 or key in values:
                raise VerificationError('invalid legacy identity configuration')
            values[key] = parts[0]
    selected = selected.expanduser().resolve()
    if values.get('ATI_ENVIRONMENT_DIR'):
        configured = Path(values['ATI_ENVIRONMENT_DIR']).expanduser().resolve()
        if configured != selected:
            raise VerificationError('legacy configured identity differs from selected identity')
    # Explicit project and empty dotenv: neither arbitrary env-file contents nor
    # ambient COMPOSE_PROJECT_NAME may redirect this read-only lookup.
    env = dict(os.environ, ATI_ENVIRONMENT_DIR=str(selected))
    result = subprocess.run(
        ['docker', 'compose', '--env-file', '/dev/null', '--project-name',
         'ati-local-research', '--project-directory', str(root), '-f',
         str(root / 'docker-compose.research.yml'), 'ps', '--all', '--quiet',
         'research-worker'], capture_output=True, text=True, env=env, check=True,
        timeout=30,
    )
    ids = result.stdout.split()
    if len(ids) != 1 or not all(c in '0123456789abcdef' for c in ids[0]) or len(ids[0]) < 12:
        raise VerificationError('expected exactly one existing research-worker container')
    # Select only necessary fields, never container environment/credentials.
    template = ('{"id":{{json .Id}},"state":{{json .State.Status}},'
                '"running":{{json .State.Running}},"labels":{{json .Config.Labels}},'
                '"mounts":{{json .Mounts}}}')
    result = subprocess.run(['docker', 'inspect', '--format', template, ids[0]],
                            capture_output=True, text=True, check=True, timeout=30)
    obj = json.loads(result.stdout)
    labels = obj.get('labels') or {}
    if not str(obj.get('id', '')).startswith(ids[0]) or obj.get('state') != 'running' or obj.get('running') is not True:
        raise VerificationError('existing connector is not running; no automatic recovery attempted')
    if (labels.get('com.docker.compose.project') != 'ati-local-research'
            or labels.get('com.docker.compose.service') != 'research-worker'
            or labels.get('com.docker.compose.oneoff', '').lower() != 'false'):
        raise VerificationError('existing container is not the expected compose service')
    mounts = [m for m in obj.get('mounts', []) if m.get('Destination') == '/var/lib/ati-environment']
    if (len(mounts) != 1 or mounts[0].get('Type') != 'bind'
            or mounts[0].get('RW') is not True
            or Path(mounts[0].get('Source', '')).resolve() != selected):
        raise VerificationError('existing connector identity mount differs from selected identity')


def main() -> int:
    try:
        verify(Path(sys.argv[1]).resolve(), Path(sys.argv[2]))
    except VerificationError as exc:
        print(f'Existing connector not retained: {exc}. No second writer started.', file=sys.stderr)
        return 3
    except (OSError, ValueError, subprocess.SubprocessError, IndexError, TypeError):
        # Do not print raw Docker output/configuration, which may contain private paths.
        print('Existing connector could not be verified (configuration, count, state, labels or identity mount). '
              'No second writer started; inspect the existing connector status before upgrading it.', file=sys.stderr)
        return 3
    print('Existing running connector with matching identity retained; no second writer started. '
          'This confirms writer presence only, not full health or new Studio capabilities.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
