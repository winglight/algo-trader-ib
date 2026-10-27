"""Run the real backup function only; never run setup, Docker, or network calls."""
import os
from pathlib import Path
import subprocess

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts' / 'install.sh'


def run_backup(tmp_path):
    installed = tmp_path / 'installed'
    backup = tmp_path / 'backup'
    backup.mkdir(exist_ok=True)
    manifest = tmp_path / 'manifest'
    source = SOURCE.read_text()
    function = source[source.index('backup_env_files() '):source.index('\nrestore_env_files() {')]
    script = "set -euo pipefail\nIFS=$'\\n\\t'\n" + function + '\nbackup_env_files "$BACKUP" "$MANIFEST"\n'
    result = subprocess.run(['bash', '-c', script], env={**os.environ,
        'INSTALL_DIR': str(installed), 'BACKUP': str(backup), 'MANIFEST': str(manifest)},
        capture_output=True, text=True, timeout=10)
    return result, backup, manifest


def require_nonroot():
    if os.geteuid() == 0:
        pytest.skip('permission-denied regression must run as a non-root user')


@pytest.mark.parametrize('runtime', ['data', 'logs', 'strategies', 'middle/data', 'unrelated', 'config/nested'])
def test_runtime_subtrees_are_not_scanned(tmp_path, runtime):
    require_nonroot()
    root = tmp_path / 'installed'
    protected = root / runtime / 'maintenance' / 'ib-total-shares' / 'fixture'
    protected.mkdir(parents=True)
    sensitive = protected / 'private.env'
    sensitive.write_text('must remain private')
    (root / '.env').write_text('APP=preserved\n')
    protected.chmod(0)
    try:
        result, backup, manifest = run_backup(tmp_path)
        assert result.returncode == 0, result.stderr
        assert 'Permission denied' not in result.stderr
        assert manifest.read_text().splitlines() == ['.env']
        assert (backup / '.env').read_text() == 'APP=preserved\n'
        assert protected.stat().st_mode & 0o777 == 0
    finally:
        protected.chmod(0o700)
    assert sensitive.read_text() == 'must remain private'


def test_only_direct_envs_in_three_configuration_directories_are_backed_up(tmp_path):
    root = tmp_path / 'installed'
    expected = ['.env', '.research.env', 'custom.env', 'middle/.env', 'middle/custom.env', 'config/.env', 'config/service.env']
    ignored = ['services/data/app.env', 'database/app.env', 'config/nested/private.env', 'middle/nested/private.env', '.env.example']
    for name in expected + ignored:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture:' + name)
    result, backup, manifest = run_backup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert set(manifest.read_text().splitlines()) == set(expected)
    for name in expected:
        assert (backup / name).read_text() == (root / name).read_text()


def test_symlinked_directory_is_not_followed(tmp_path):
    root = tmp_path / 'installed'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'private.env').write_text('outside scope')
    (root / 'linked').symlink_to(outside, target_is_directory=True)
    (root / '.env').write_text('APP=fixture')
    result, backup, manifest = run_backup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert manifest.read_text().splitlines() == ['.env']
    assert not (backup / 'linked').exists()
    assert (outside / 'private.env').read_text() == 'outside scope'


@pytest.mark.parametrize('blocked', ['directory', 'env_file'])
def test_application_permission_errors_are_not_hidden(tmp_path, blocked):
    require_nonroot()
    root = tmp_path / 'installed'
    app = root / 'config'
    app.mkdir(parents=True)
    env = app / 'required.env'
    env.write_text('required configuration')
    denied = app if blocked == 'directory' else env
    denied.chmod(0)
    try:
        result, _, _ = run_backup(tmp_path)
        assert result.returncode != 0
        assert 'Permission denied' in result.stderr or 'Cannot read configuration directory' in result.stderr
        assert denied.stat().st_mode & 0o777 == 0
    finally:
        denied.chmod(0o700 if blocked == 'directory' else 0o600)
    assert env.read_text() == 'required configuration'


@pytest.mark.parametrize('symlink_runtime', [False, True])
def test_replacement_preserves_inaccessible_runtime_and_configuration(tmp_path, symlink_runtime):
    require_nonroot()
    root = tmp_path / 'installed'
    root.mkdir()
    data = tmp_path / 'external-data' if symlink_runtime else root / 'data'
    blocked = data / 'maintenance' / 'ib-total-shares' / 'fixture'
    blocked.mkdir(parents=True)
    private = blocked / 'private.env'
    private.write_text('untouched runtime data')
    if symlink_runtime:
        (root / 'data').symlink_to(data, target_is_directory=True)
    (root / '.env').write_text('APP=local\n')
    (root / 'middle').mkdir()
    (root / 'middle' / '.env').write_text('MIDDLE=local\n')
    (root / 'strategies').mkdir()
    (root / 'strategies' / 'local.py').write_text('# local strategy\n')
    candidate = tmp_path / 'candidate'
    (candidate / 'middle').mkdir(parents=True)
    (candidate / 'data').mkdir()
    (candidate / 'data' / 'sample').write_text('release sample, not runtime data')
    (candidate / '.env').write_text('APP=release\n')
    (candidate / 'application.txt').write_text('new application')
    backup = tmp_path / 'env-backup'
    backup.mkdir()
    runtime = tmp_path / 'runtime-backup'
    runtime.mkdir()
    manifest = tmp_path / 'manifest'
    # Definitions only: no download, setup, service commands, or privileged calls.
    definitions = SOURCE.read_text().split('\nprompt_install_dir\n')[0]
    script = definitions + '''
run_as_root() { "$@"; }
backup_env_files "$BACKUP" "$MANIFEST"
replace_install_dir_contents "$CANDIDATE" "$BACKUP" "$MANIFEST" "$RUNTIME_BACKUP"
find "$RUNTIME_BACKUP" -depth -type d -empty -delete
'''
    blocked.chmod(0)
    try:
        result = subprocess.run(['bash', '-c', script], env={**os.environ,
            'ATI_INSTALL_DIR': str(root), 'BACKUP': str(backup), 'MANIFEST': str(manifest),
            'CANDIDATE': str(candidate), 'RUNTIME_BACKUP': str(runtime)},
            capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
        assert blocked.stat().st_mode & 0o777 == 0
        assert (root / 'data').is_symlink() == symlink_runtime
        assert not runtime.exists()
    finally:
        blocked.chmod(0o700)
    assert private.read_text() == 'untouched runtime data'
    assert not (root / 'data' / 'sample').exists()
    assert (root / '.env').read_text() == 'APP=local\n'
    assert (root / 'middle' / '.env').read_text() == 'MIDDLE=local\n'
    assert (root / 'strategies' / 'local.py').read_text() == '# local strategy\n'
    assert (root / 'application.txt').read_text() == 'new application'


@pytest.mark.parametrize('directory', ['middle', 'config'])
@pytest.mark.parametrize('state', ['missing', 'file', 'unreadable', 'symlink'])
def test_fixed_optional_directory_boundaries(tmp_path, directory, state):
    require_nonroot()
    root = tmp_path / 'installed'
    root.mkdir()
    (root / '.env').write_text('APP=fixture')
    target = root / directory
    if state == 'file':
        target.write_text('not a configuration directory')
    elif state == 'unreadable':
        target.mkdir()
        target.chmod(0)
    elif state == 'symlink':
        outside = tmp_path / 'outside'
        outside.mkdir()
        (outside / 'private.env').write_text('outside scope')
        target.symlink_to(outside, target_is_directory=True)
    try:
        result, backup, _ = run_backup(tmp_path)
        if state == 'missing':
            assert result.returncode == 0, result.stderr
            assert (backup / '.env').read_text() == 'APP=fixture'
        else:
            assert result.returncode != 0
            assert 'configuration directory' in result.stderr
    finally:
        if state == 'unreadable':
            target.chmod(0o700)
    if state == 'symlink':
        assert (outside / 'private.env').read_text() == 'outside scope'
        assert not (backup / directory / 'private.env').exists()


@pytest.mark.parametrize('relative', ['.env', '.research.env', 'middle/local.env', 'config/local.env'])
def test_selected_env_symlink_is_rejected_without_reading_target(tmp_path, relative):
    root = tmp_path / 'installed'
    source = root / relative
    source.parent.mkdir(parents=True)
    outside = tmp_path / 'outside.env'
    outside.write_text('outside scope')
    source.symlink_to(outside)
    result, backup, _ = run_backup(tmp_path)
    assert result.returncode != 0
    assert 'Refusing to follow configuration symlink' in result.stderr
    assert not (backup / relative).exists()
    assert outside.read_text() == 'outside scope'


def test_backup_has_no_directory_traversal_command():
    source = SOURCE.read_text()
    function = source[source.index('backup_env_files() '):source.index('\nrestore_env_files() {')]
    assert '\n    find ' not in function
    assert 'for relative_dir in . middle config' in function
