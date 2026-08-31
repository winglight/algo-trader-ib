from pathlib import Path


INSTALLER = Path(__file__).resolve().parents[1] / "setup_and_run.sh"


def test_installer_stops_in_red_without_runtime_rollback() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert "set -Eeuo pipefail" in source
    assert "trap stop_on_install_error ERR" in source
    assert "trap finish_install EXIT" in source
    assert 'print_error "Installation stopped immediately."' in source
    assert "No automatic rollback was attempted" in source
    assert "Installation failed; restoring previous environment files." not in source
    assert "rollback()" not in source
    assert "trap rollback EXIT" not in source
