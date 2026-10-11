from pathlib import Path
import os
import shutil
import subprocess
import zipfile

import pytest


INSTALLER = Path(__file__).resolve().parents[1] / "setup_and_run.sh"


def test_installer_stops_in_red_without_runtime_rollback() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert "set -euo pipefail" in source
    assert "trap stop_on_install_error ERR" in source
    assert "trap finish_install EXIT" in source
    assert 'print_error "Installation stopped immediately."' in source
    assert "No automatic rollback was attempted" in source
    assert "Installation failed; restoring previous environment files." not in source
    assert "rollback()" not in source
    assert "trap rollback EXIT" not in source


def test_broker_profile_bootstrap_has_app_on_pythonpath() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    compose = (INSTALLER.parent / "docker-compose.yml").read_text(encoding="utf-8")

    assert "python scripts/maintenance/bootstrap_broker_profiles.py" in installer
    assert "PYTHONPATH: /app:" in compose


@pytest.mark.parametrize("compatible", [False, True])
def test_update_checks_options_before_stopping_or_replacing(tmp_path, compatible):
    root = INSTALLER.parent
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "scripts").mkdir()
    for name in ("options_upgrade_guard.sh", "options_upgrade_guard.py"):
        shutil.copy(root / "scripts" / name, installed / "scripts" / name)
    (installed / "docker-compose.yml").write_text("services: {}\n")
    (installed / "algo_trader.sql").write_text((root / "algo_trader.sql").read_text())
    (installed / "current-runtime").write_text("preserve protection")
    archive = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("algo-trader-ib-candidate/algo_trader.sql", (root / "algo_trader.sql").read_text() if compatible else "CREATE TABLE legacy(id INT);\n")
        bundle.writestr("algo-trader-ib-candidate/setup_and_run.sh", '#!/bin/bash\nprintf "setup\\n" >> "$GUARD_LOG"\n')
    commands = tmp_path / "bin"
    commands.mkdir()
    docker = commands / "docker"
    docker.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$GUARD_LOG"
case "$*" in
  *"exec -T orders-service python3 -"*) cat >/dev/null; echo '{"ready":false,"counts":{"lots":1}}'; exit 1 ;;
esac
''')
    curl = commands / "curl"
    curl.write_text('''#!/bin/bash
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then cp "$GUARD_ARCHIVE" "$2"; exit; fi
  shift
done
exit 1
''')
    docker.chmod(0o755)
    curl.chmod(0o755)
    log = tmp_path / "actions.log"
    result = subprocess.run(["bash", str(root / "scripts/install.sh"), "--update", "--non-interactive"],
        env={**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"], "ATI_INSTALL_DIR": str(installed),
            "ATI_PUBLIC_ARCHIVE_URL": "https://fixture.invalid/candidate.zip", "ATI_ALLOW_UPDATE": "1",
            "GUARD_LOG": str(log), "GUARD_ARCHIVE": str(archive)}, capture_output=True, text=True, timeout=15)
    actions = log.read_text()
    if compatible:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "down" in actions and "setup" in actions and "exec -T orders-service" not in actions
        assert not (installed / "current-runtime").exists()
    else:
        assert result.returncode != 0 and "OPTIONS_SCHEMA_DOWNGRADE_BLOCKED" in result.stderr
        assert "exec -T orders-service" in actions and "down" not in actions and "setup" not in actions
        assert (installed / "current-runtime").read_text() == "preserve protection"
