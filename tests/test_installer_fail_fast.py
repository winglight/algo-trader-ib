from pathlib import Path


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
