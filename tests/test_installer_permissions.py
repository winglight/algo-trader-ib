"""Run installer functions only, against isolated files and mocked system tools."""
import os
from pathlib import Path
import pty
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "setup_and_run.sh").read_text()


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / "installation with spaces"
    root.mkdir()
    (root / "data").mkdir()
    prepared = root / "data/.candidate"
    prepared.mkdir()
    (prepared / "new-plugin").write_text("new")
    (root / ".env").write_text("unchanged configuration")
    bins = tmp_path / "bin"
    bins.mkdir()
    log = tmp_path / "system-calls"
    # No tests can invoke actual sudo, containers, downloads or ownership changes.
    for name in ("sudo", "chown", "docker", "curl"):
        command = bins / name
        command.write_text('#!/bin/bash\nprintf "%s\\n" "$0 $*" >> "$CALLS"\nexit 97\n')
        command.chmod(0o755)
    functions = "\n".join(re.findall(r"^[a-zA-Z_][a-zA-Z_0-9]*\(\) \{\n.*?^\}", SOURCE, re.M | re.S))
    functions += "\n" + re.search(r"^has_cmd\(\).*", SOURCE, re.M).group()
    prelude = '''set -euo pipefail
ROOT_DIR="$TEST_ROOT"
MIDDLE_DIR="$ROOT_DIR/middle"
PREPARED_PLUGIN_DIR="$ROOT_DIR/data/.candidate"
PREPARED_PLUGIN_BUILD_DIR=""
PLUGIN_BACKUP_DIR=""
PLUGIN_ACTIVATED=0
ROOT_CANDIDATE=""; MIDDLE_CANDIDATE=""; SCREENERS_CANDIDATE=""
NON_INTERACTIVE=1
RED=''; RESET=''; INSTALL_ERROR_REPORTED=0
ENABLED_ADAPTERS=sim
id() { case "$1" in -u) echo 501 ;; -g) echo 20 ;; esac; }
contains_profile() { return 1; }
'''

    def run(body, answer=None):
        kwargs = {}
        master = slave = None
        if answer is not None:
            master, slave = pty.openpty()
            os.write(master, (answer + "\n").encode())
            kwargs["stdin"] = slave
        try:
            return subprocess.run(["bash", "-c", functions + "\n" + prelude + body],
                env={**os.environ, "PATH": str(bins) + os.pathsep + os.environ["PATH"],
                     "TEST_ROOT": str(root), "CALLS": str(log)},
                capture_output=True, text=True, timeout=10, **kwargs)
        finally:
            if master is not None:
                os.close(master)
                os.close(slave)

    return root, log, run


def no_system_calls(log):
    assert not log.exists(), log.read_text() if log.exists() else ""


@pytest.mark.parametrize("existing", [False, True])
def test_user_owned_activation_marker_and_cleanup_need_no_sudo(fixture, existing):
    root, log, run = fixture
    if existing:
        (root / "data/broker-plugins").mkdir()
        (root / "data/broker-plugins/old-plugin").write_text("old")
    result = run('''activate_prepared_plugins
test "$PLUGIN_ACTIVATED" = 1
test -z "$PREPARED_PLUGIN_DIR"
mark_shared_network_migrated
cleanup_candidates
''')
    assert result.returncode == 0, result.stderr
    assert (root / "data/broker-plugins/new-plugin").read_text() == "new"
    assert (root / "data/.shared-network-external-v1").exists()
    assert (root / "data/connector").is_dir()
    if existing:
        assert next((root / "data").glob(".broker-plugins.previous.*/plugins/old-plugin")).read_text() == "old"
    assert (root / ".env").read_text() == "unchanged configuration"
    no_system_calls(log)


@pytest.mark.parametrize("operation", ["mkdir", "touch", "backup", "activate", "restore"])
def test_file_errors_are_preserved_without_privileged_retry(fixture, operation):
    root, log, run = fixture
    live = root / "data/broker-plugins"
    live.mkdir()
    (live / "old-plugin").write_text("old")
    faults = {
        "mkdir": 'mkdir() { echo "read-only filesystem" >&2; return 30; }; activate_prepared_plugins',
        "touch": 'touch() { echo "disk full" >&2; return 28; }; mark_shared_network_migrated',
        "backup": 'mv() { echo "permission denied (legacy root-owned directory)" >&2; return 13; }; activate_prepared_plugins',
        "activate": '''mv() { if [ "$1" = "$PREPARED_PLUGIN_DIR" ]; then echo "rename I/O failure" >&2; return 5; fi; command mv "$@"; }; activate_prepared_plugins''',
        "restore": '''mv() { if [ "$1" = "$PREPARED_PLUGIN_DIR" ]; then return 5; fi; case "$1" in */plugins) return 13 ;; esac; command mv "$@"; }; activate_prepared_plugins''',
    }
    result = run(faults[operation])
    assert result.returncode == {"mkdir": 30, "touch": 28, "backup": 13, "activate": 5, "restore": 5}[operation], result.stderr
    assert (root / "data/.candidate/new-plugin").exists()
    if operation == "restore":
        assert "Previous Adapter plugins retained at" in result.stderr
        old = next((root / "data").glob(".broker-plugins.previous.*/plugins/old-plugin"))
    else:
        old = live / "old-plugin"
    assert old.read_text() == "old"
    if operation == "activate":
        assert "Previous Adapter plugins restored" in result.stderr
    assert (root / ".env").read_text() == "unchanged configuration"
    no_system_calls(log)


def test_actual_unwritable_directory_never_elevates(fixture):
    if os.getuid() == 0:
        pytest.skip("real permission denial needs an unprivileged test runner")
    root, log, run = fixture
    (root / "data").chmod(0o500)
    try:
        result = run("activate_prepared_plugins")
        assert result.returncode != 0
        assert (root / "data/.candidate/new-plugin").exists()
        no_system_calls(log)
    finally:
        (root / "data").chmod(0o700)


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling"])
def test_existing_unexpected_plugin_path_is_preserved(fixture, kind):
    root, log, run = fixture
    live = root / "data/broker-plugins"
    if kind == "file":
        live.write_text("preserve")
    else:
        live.symlink_to(root / ("data/.candidate" if kind == "symlink" else "missing"))
    result = run("activate_prepared_plugins")
    assert result.returncode != 0
    assert live.is_symlink() if kind != "file" else live.read_text() == "preserve"
    assert (root / "data/.candidate/new-plugin").exists()
    no_system_calls(log)


def test_preparation_and_cleanup_are_unprivileged_and_on_target_filesystem(fixture):
    root, log, run = fixture
    result = run('''prepare_selected_adapter_plugins fixture-image
case "$PREPARED_PLUGIN_DIR" in "$ROOT_DIR"/data/.broker-plugins.candidate.*) ;; *) exit 99 ;; esac
test -d "$PREPARED_PLUGIN_DIR"
cleanup_candidates
test ! -e "$PREPARED_PLUGIN_DIR"
''')
    assert result.returncode == 0, result.stderr
    no_system_calls(log)


@pytest.mark.parametrize("stage", ["download", "build"])
def test_failed_plugin_preparation_preserves_live_plugins_and_cleans_candidate(fixture, stage):
    root, log, run = fixture
    live = root / "data/broker-plugins"
    live.mkdir()
    (live / "old-plugin").write_text("old")
    before = ""
    if stage == "build":
        before = '''curl() { while [ "$1" != -o ]; do shift; done; printf fixture > "$2"; }
tar() { return 0; }
'''
    result = run(before + '''ADAPTERS_ARCHIVE_URL=https://fixture.invalid/adapters.tar.gz
ADAPTERS_REPOSITORY=fixture; ADAPTERS_REF=fixture
ADAPTERS_ARCHIVE_SHA256=''
contains_profile() { [ "$2" = ibkr_paper ]; }
trap finish_install EXIT
prepare_selected_adapter_plugins fixture-image
''')
    assert result.returncode == 97, result.stderr
    assert (live / "old-plugin").read_text() == "old"
    assert not list((root / "data").glob(".broker-plugins.candidate.*"))
    assert not list(root.glob(".adapter-plugin-build.*"))
    assert "sudo" not in log.read_text()
    assert ("docker" in log.read_text()) == (stage == "build")


@pytest.mark.parametrize("platform", ["Linux", "Darwin"])
@pytest.mark.parametrize("owner", ["1000:1000", "0:0"])
def test_gateway_stat_and_noninteractive_root_operation(fixture, platform, owner):
    root, log, run = fixture
    stat = f'''stat() {{
if [ "$1" = -c ] && [ {platform} = Darwin ]; then echo 'illegal option -- c' >&2; return 1; fi
echo '{owner}'
}}
'''
    result = run(stat + "ensure_ib_gateway_settings_permissions")
    assert (result.returncode == 0) == (owner == "1000:1000"), result.stderr
    assert "illegal option" not in result.stderr
    if owner != "1000:1000":
        assert "IB Gateway container settings ownership" in result.stderr
        assert "non-interactive" in result.stderr
    no_system_calls(log)


@pytest.mark.parametrize("noninteractive", [0, 1])
def test_no_tty_or_no_sudo_never_prompts_or_runs_privileged_command(fixture, noninteractive):
    _, log, run = fixture
    result = run(f'''NON_INTERACTIVE={noninteractive}
has_cmd() {{ return 1; }}
run_as_root 'Set fixture system ownership' chown 1000:1000 fixture
''')
    assert result.returncode != 0
    assert "Set fixture system ownership" in result.stderr
    no_system_calls(log)


def test_root_keeps_system_operation_semantics(fixture):
    _, log, run = fixture
    result = run('''id() { echo 0; }
run_as_root 'fixture system operation' chown 1000:1000 fixture
''')
    assert result.returncode == 97
    assert "chown 1000:1000 fixture" in log.read_text()
    assert "sudo" not in log.read_text()


@pytest.mark.parametrize("answer", ["y", "n", ""])
def test_interactive_system_operation_requires_explained_opt_in(fixture, answer):
    _, log, run = fixture
    result = run('''NON_INTERACTIVE=0
run_as_root 'Set fixture container ownership' chown 1000:1000 fixture
''', answer=answer)
    assert result.returncode == (97 if answer == "y" else 1)
    assert "local computer administrator/login password" in result.stderr
    assert "Typed characters are not displayed" in result.stderr
    if answer == "y":
        assert "sudo -p ATI system operation" in log.read_text()
    else:
        assert "cancelled" in result.stderr
        no_system_calls(log)


def test_interactive_missing_sudo_is_actionable(fixture):
    _, log, run = fixture
    result = run('''NON_INTERACTIVE=0
has_cmd() { return 1; }
run_as_root 'Set fixture container ownership' chown 1000:1000 fixture
''', answer="n")
    assert result.returncode == 1
    assert "sudo is unavailable" in result.stderr
    no_system_calls(log)


def test_actual_mkdir_failure_and_missing_candidate_preserve_files(fixture):
    root, log, run = fixture
    result = run('''run_install_operation 'create directory' mkdir "$ROOT_DIR/.env"''')
    assert result.returncode != 0
    assert (root / ".env").read_text() == "unchanged configuration"
    result = run('run_install_operation "move missing fixture" mv "$ROOT_DIR/missing" "$ROOT_DIR/.env"')
    assert result.returncode != 0
    assert (root / ".env").read_text() == "unchanged configuration"
    result = run('PREPARED_PLUGIN_DIR="$ROOT_DIR/missing"; activate_prepared_plugins')
    assert result.returncode != 0
    assert "missing or unsafe" in result.stderr
    assert not list((root / "data").glob(".broker-plugins.previous.*"))
    no_system_calls(log)


@pytest.mark.parametrize("original_rc", [0, 5])
def test_cleanup_failure_is_reported_without_masking_original_failure(fixture, original_rc):
    _, log, run = fixture
    result = run(f'''rm() {{ echo 'fixture removal failed' >&2; return 13; }}
trap finish_install EXIT
exit {original_rc}
''')
    assert result.returncode == (original_rc or 13)
    assert "remove temporary Adapter plugins" in result.stderr
    assert "fixture removal failed" in result.stderr
    no_system_calls(log)


def test_fresh_user_owned_data_prepares_nested_mountpoint_without_sudo(fixture):
    root, log, run = fixture
    (root / "data/.candidate").rename(root / ".saved-candidate")
    (root / "data").rmdir()
    result = run("ensure_install_data_directory")
    assert result.returncode == 0, result.stderr
    target = root / "data/connector"
    assert target.is_dir() and not list(target.iterdir())
    assert target.stat().st_uid == os.getuid()
    assert (root / "data").stat().st_uid == os.getuid()
    no_system_calls(log)


def test_nested_mountpoint_preserves_existing_files_and_external_connector(fixture):
    root, log, run = fixture
    target = root / "data/connector"
    target.mkdir()
    (target / "existing-file").write_text("preserve mount target contents")
    external = root / "external-environment/trading"
    external.mkdir(parents=True)
    (external / "fixture-grant").write_text("fixture source remains outside data")
    result = run('ATI_ENVIRONMENT_DIR="$ROOT_DIR/external-environment"; ensure_install_data_directory; ensure_install_data_directory')
    assert result.returncode == 0, result.stderr
    assert (target / "existing-file").read_text() == "preserve mount target contents"
    assert not (target / "fixture-grant").exists()
    assert (external / "fixture-grant").read_text() == "fixture source remains outside data"
    no_system_calls(log)


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling"])
def test_nested_mountpoint_rejects_unexpected_existing_path(fixture, kind):
    root, log, run = fixture
    target = root / "data/connector"
    if kind == "file":
        target.write_text("preserve")
    else:
        target.symlink_to(root / ("data/.candidate" if kind == "symlink" else "missing"))
    result = run("prepare_selected_adapter_plugins fixture-image")
    assert result.returncode == 1
    assert "Connector mount target must be a regular directory" in result.stderr
    assert target.is_symlink() if kind != "file" else target.read_text() == "preserve"
    assert not list((root / "data").glob(".broker-plugins.candidate.*"))
    no_system_calls(log)


def test_unwritable_legacy_data_stops_before_mountpoint_or_plugin_changes(fixture):
    if os.getuid() == 0:
        pytest.skip("permission denial requires an unprivileged test runner")
    root, log, run = fixture
    (root / "data").chmod(0o555)
    try:
        result = run("prepare_selected_adapter_plugins fixture-image")
        assert result.returncode == 1
        assert "not writable/searchable" in result.stderr
        assert "owner/administrator" in result.stderr
        assert not (root / "data/connector").exists()
        assert not list((root / "data").glob(".broker-plugins.candidate.*"))
        no_system_calls(log)
    finally:
        (root / "data").chmod(0o700)


def test_nested_mountpoint_failure_preserves_exit_code(fixture):
    root, log, run = fixture
    result = run('''mkdir() {
case "${*: -1}" in */data/connector) echo 'fixture mkdir denied' >&2; return 13 ;; esac
command mkdir "$@"
}
prepare_selected_adapter_plugins fixture-image
''')
    assert result.returncode == 13
    assert "prepare nested connector mount target" in result.stderr
    assert not (root / "data/connector").exists()
    assert not list((root / "data").glob(".broker-plugins.candidate.*"))
    no_system_calls(log)


def test_compose_nested_bind_contract_precedes_application_startup():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "- ./data:/app/data" in compose
    assert "/trading:/app/data/connector:ro" in compose
    assert SOURCE.index("prepare_selected_adapter_plugins \"$broker_runner_image\"") < SOURCE.index("pull_application_images\n")
