from pathlib import Path
import subprocess

from dotenv import dotenv_values
import pytest


LIBRARY = Path(__file__).resolve().parents[1] / "scripts/installer_lib.sh"


@pytest.mark.parametrize("secret", ["random$TOKEN", "$2b$12$hash", "${LITERAL}$$", "dollar$with'quote\\and space"])
def test_installer_preserves_literal_dollar_credentials(tmp_path, secret):
    path = tmp_path / ".env"
    path.touch()
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; env_set_quoted "$2" TEST_SECRET "$3"; read_env_value "$2" TEST_SECRET',
         "env-test", str(LIBRARY), str(path), secret],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout == secret
    assert dict(dotenv_values(path, interpolate=False)) == {"TEST_SECRET": secret}
    assert path.stat().st_mode & 0o777 == 0o600
