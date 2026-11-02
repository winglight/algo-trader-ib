"""Exercise the real backup pipeline and the pre-stop disk-space gate."""
import gzip
import os
import shutil
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def executable(path, source):
    path.write_text('#!/bin/bash\n' + source)
    path.chmod(0o755)


@pytest.mark.parametrize('failure', ['', 'dump', 'empty', 'compress', 'corrupt'])
def test_compressed_backup_requires_complete_dump_and_valid_stream(tmp_path, failure):
    middle = tmp_path / 'runtime' / 'middle'
    middle.mkdir(parents=True)
    (middle / '.env').write_text('fixture only\n')
    commands = tmp_path / 'bin'
    commands.mkdir()
    executable(commands / 'docker', '''
case "$*" in
  *'ps -q mariadb') echo fixture-container ;;
  *'exec -T mariadb'*)
    read -r password
    [ "$FAILURE" != empty ] || exit 0
    printf '%s\\n' 'CREATE TABLE fixture(id INT);'
    [ "$FAILURE" != dump ] || exit 17
    ;;
  *'config') echo 'services: {}' ;;
  *) exit 20 ;;
esac
''')
    if failure == 'compress':
        executable(commands / 'gzip', 'cat >/dev/null\nexit 18\n')
    elif failure == 'corrupt':
        executable(commands / 'gzip', '''
if [ "$1" = -1 ]; then cat >/dev/null; echo corrupt; exit 0; fi
exec "$REAL_GZIP" "$@"
''')
    source = (ROOT / 'setup_and_run.sh').read_text()
    function = 'backup_database_for_update() {' + source.split('backup_database_for_update() {', 1)[1].split('\nif [ -z "$COMPONENTS" ]', 1)[0]
    backup = tmp_path / 'backups'
    result = subprocess.run(['bash', '-c', 'set -eu\n' + function + '\nbackup_database_for_update'],
        env={**os.environ, 'PATH': str(commands) + os.pathsep + os.environ['PATH'],
             'ROOT_DIR': str(middle.parent), 'MIDDLE_DIR': str(middle), 'UPDATE_MODE': '1',
             'ATI_BACKUP_DIR': str(backup), 'MARIADB_PASSWORD': 'fixture-secret',
             'FAILURE': failure, 'REAL_GZIP': shutil.which('gzip')},
        capture_output=True, text=True, timeout=10)
    assert 'fixture-secret' not in result.stdout + result.stderr
    completed = list(backup.glob('*/BACKUP_COMPLETE'))
    assert not list(backup.glob('*/*.partial'))
    if failure:
        assert result.returncode != 0
        assert not completed
        assert not list(backup.glob('*/all-databases.sql.gz'))
    else:
        assert result.returncode == 0, result.stderr
        assert len(completed) == 1
        dump = Path(completed[0].read_text().strip())
        assert dump.name == 'all-databases.sql.gz'
        assert gzip.decompress(dump.read_bytes()) == b'CREATE TABLE fixture(id INT);\n'
        assert dump.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('available', [0, 1048575, 1048576])
def test_update_space_gate_checks_backup_filesystem(tmp_path, available):
    commands = tmp_path / 'bin'
    commands.mkdir()
    install = tmp_path / 'installed'
    install.mkdir()
    backup = tmp_path / 'backup'
    backup.mkdir()
    executable(commands / 'df', '''
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'
if [ "$2" = "$INSTALL" ]; then free=9999999; else free="$FREE"; fi
printf 'fixture 99999999 1 %s 1%% /\\n' "$free"
''')
    result = subprocess.run(['bash', '-c', 'set -euo pipefail\nsource "$LIB"\ncheck_update_storage "$INSTALL"'],
        env={**os.environ, 'PATH': str(commands) + os.pathsep + os.environ['PATH'],
             'LIB': str(ROOT / 'scripts/installer_lib.sh'), 'INSTALL': str(install),
             'ATI_BACKUP_DIR': str(backup), 'FREE': str(available), 'ATI_UPDATE_MIN_FREE_MB': '1024'},
        capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) == (available >= 1048576)
    if available < 1048576:
        assert str(backup) in result.stderr
        assert 'Insufficient disk space' in result.stderr


@pytest.mark.parametrize("bootstrap", [False, True])
def test_insufficient_space_aborts_installer_before_download_or_stopping(tmp_path, bootstrap):
    install = tmp_path / 'installed'
    install.mkdir()
    (install / '.env').write_text('APP=fixture\n')
    commands = tmp_path / 'bin'
    commands.mkdir()
    executable(commands / 'df', "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\nfixture 9999 9999 0 100%% /\\n'\n")
    for name in ('docker', 'curl'):
        executable(commands / name, 'echo touched >> "$ACTIONS"\nexit 1\n')
    actions = tmp_path / 'actions'
    invocation = (['bash', '-c', (ROOT / 'scripts/install.sh').read_text(), 'installer']
                  if bootstrap else ['bash', str(ROOT / 'scripts/install.sh')])
    result = subprocess.run(invocation + ['--update', '--non-interactive'],
        env={**os.environ, 'PATH': str(commands) + os.pathsep + os.environ['PATH'],
             'ATI_INSTALL_DIR': str(install), 'ATI_ALLOW_UPDATE': '1', 'ATI_UPDATE_MIN_FREE_MB': '1024',
             'ATI_BACKUP_DIR': str(tmp_path / 'backup'), 'ACTIONS': str(actions)},
        capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert 'Insufficient disk space' in result.stderr
    assert not actions.exists()
    assert (install / '.env').read_text() == 'APP=fixture\n'
