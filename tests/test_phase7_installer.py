from __future__ import annotations

import os
from pathlib import Path
import json
import shutil
import stat
import subprocess

import pytest


PUBLIC_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_ARCHIVE_SHA256 = "e9481d3a411e5907d51204beeb85426cfb758c4587fc894c8661f8979d6b174e"
ALPACA_WHEEL_SHA256 = "0b4cac9b743851310f19f6a9aa84f57ddf95ae75b601350395746a893f54a2da"
SECRET_VALUES = {
    "redis": "phase7-redis-secret",
    "mariadb": "phase7-mariadb-secret",
    "admin": "phase7-admin-secret",
    "ib_user": "phase7-ib-user",
    "ib_password": "phase7-ib-secret",
    "ib_vnc": "phase7-vnc-secret",
    "alpaca_key": "phase7-alpaca-key",
    "alpaca_secret": "phase7-alpaca-secret",
}


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture()
def runtime(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    root = tmp_path / "runtime"
    shutil.copytree(
        PUBLIC_ROOT,
        root,
        ignore=shutil.ignore_patterns(".git", ".env", ".ati-adapter-build", "data", "logs"),
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    _write_executable(
        fake_bin / "docker",
        """#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
if [ "${1:-}" = "info" ]; then
  if [ "${2:-}" = "--format" ]; then printf 'aarch64\n'; fi
  exit 0
fi
if [ "${1:-}" = "compose" ] && [ "${2:-}" = "version" ]; then exit 0; fi
if [ "${1:-}" = "compose" ] && printf '%s' "$*" | grep -q ' config -q'; then
  [ "${FAKE_FAIL_COMPOSE:-0}" = "1" ] && exit 31
  exit 0
fi
if [ "${1:-}" = "run" ]; then
  if [ "${FAKE_FAIL_ENTRYPOINT:-0}" = "1" ] && printf '%s' "$*" | grep -q 'AdapterProfileRegistry'; then
    exit 32
  fi
  exit 0
fi
if [ "${1:-}" = "compose" ] && printf '%s' "$*" | grep -q ' exec -T mariadb'; then
  cat >/dev/null
  exit 0
fi
if [ "${FAKE_FAIL_APP_UP:-0}" = "1" ] \
  && printf '%s' "$*" | grep -q 'docker-compose.yml up -d' \
  && ! printf '%s' "$*" | grep -q '/middle/docker-compose.yml'; then
  exit 42
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/bin/sh
set -eu
output=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then output="$2"; shift 2; else shift; fi
done
if [ -n "$output" ]; then printf 'fixture' > "$output"; fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "tar",
        """#!/bin/sh
set -eu
destination=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-C" ]; then destination="$2"; shift 2; else shift; fi
done
mkdir -p "$destination/packages/alpaca-paper"
printf '[build-system]\nrequires=[]\n' > "$destination/packages/alpaca-paper/pyproject.toml"
""",
    )
    _write_executable(
        fake_bin / "sha256sum",
        f"""#!/bin/sh
set -eu
if [ "${{FAKE_BAD_CHECKSUM:-0}}" = "1" ]; then
  printf '%064d  %s\n' 0 "${{1:-fixture}}"
else
  case "${{1:-}}" in
    *alpaca_py*) checksum='{ALPACA_WHEEL_SHA256}' ;;
    *pandas*) checksum='cd8d0c3be0515c12fed0bdbae072551c8b54b7192c7b1fda0ba56059a0179698' ;;
    *pytz*) checksum='5ddf76296dd8c44c26eb8f4b6f35488f3ccbf6fbbd7adee0b7262d43f0ec2f00' ;;
    *requests*) checksum='2462f94637a34fd532264295e186976db0f5d453d1cdd31473c85a6a161affb6' ;;
    *sseclient*) checksum='340062b1587fc2880892811e2ab5b176d98ef3eee98b3672ff3a3ba1e8ed0f6f' ;;
    *urllib3*) checksum='e6b01673c0fa6a13e374b50871808eb3bf7046c4b125b216f6bf1cc604cff0dc' ;;
    *charset_normalizer*) checksum='7a32c560861a02ff789ad905a2fe94e3f840803362c84fecf1851cb4cf3dc37f' ;;
    *) checksum='{ADAPTER_ARCHIVE_SHA256}' ;;
  esac
  printf '%s  %s\n' "$checksum" "${{1:-fixture}}"
fi
""",
    )
    _write_executable(fake_bin / "open", "#!/bin/sh\nexit 0\n")
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FAKE_DOCKER_LOG": str(docker_log),
            "ATI_APP_URL": "http://127.0.0.1:15999",
        }
    )
    return root, env, docker_log


def _secret_files(root: Path, names: set[str] | None = None) -> dict[str, Path]:
    selected = names or set(SECRET_VALUES)
    secret_dir = root / "test-secrets"
    secret_dir.mkdir(exist_ok=True)
    result: dict[str, Path] = {}
    for name in selected:
        path = secret_dir / name
        path.write_text(f"{SECRET_VALUES[name]}\n", encoding="utf-8")
        path.chmod(0o600)
        result[name] = path
    return result


def _base_args(files: dict[str, Path]) -> list[str]:
    return [
        "--non-interactive",
        "--redis-password-file",
        str(files["redis"]),
        "--mariadb-password-file",
        str(files["mariadb"]),
        "--admin-password-file",
        str(files["admin"]),
    ]


def _adapter_args(enabled: str, initial: str, files: dict[str, Path]) -> list[str]:
    args = ["--enabled-adapters", enabled, "--initial-adapter", initial]
    if "ibkr_paper" in enabled:
        args += [
            "--ibkr-username-file",
            str(files["ib_user"]),
            "--ibkr-password-file",
            str(files["ib_password"]),
            "--ibkr-vnc-password-file",
            str(files["ib_vnc"]),
        ]
    if "alpaca_paper" in enabled:
        args += [
            "--alpaca-data-feed",
            "iex",
            "--alpaca-api-key-id-file",
            str(files["alpaca_key"]),
            "--alpaca-secret-key-file",
            str(files["alpaca_secret"]),
        ]
    return args


def _run(
    root: Path,
    env: dict[str, str],
    args: list[str],
    *,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "setup_and_run.sh"), *args],
        cwd=root,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


@pytest.mark.parametrize(
    ("enabled", "initial"),
    [
        ("sim", "sim"),
        ("sim,ibkr_paper", "sim"),
        ("sim,ibkr_paper", "ibkr_paper"),
        ("sim,alpaca_paper", "sim"),
        ("sim,alpaca_paper", "alpaca_paper"),
        ("sim,ibkr_paper,alpaca_paper", "sim"),
        ("sim,ibkr_paper,alpaca_paper", "ibkr_paper"),
        ("sim,ibkr_paper,alpaca_paper", "alpaca_paper"),
    ],
)
def test_install_matrix(
    runtime: tuple[Path, dict[str, str], Path], enabled: str, initial: str
) -> None:
    root, env, docker_log = runtime
    files = _secret_files(root)
    result = _run(root, env, _base_args(files) + _adapter_args(enabled, initial, files))
    assert result.returncode == 0, result.stdout + result.stderr

    values = _read_env(root / ".env")
    assert values["BROKER_RUNNER_ENABLED_ADAPTERS"] == enabled
    assert values["BROKER_RUNNER_DEFAULT_ADAPTER_ID"] == initial
    assert values["BROKER_RUNNER_PROFILE_REGISTRY_ENABLED"] == "true"
    assert values["BROKER_RUNNER_IBKR_PAPER_PROVIDER"] == "core"
    assert values["BROKER_ADAPTER_SWITCH_ENABLED"] == "true"
    assert values["BROKER_ADAPTER_SWITCH_GATE_ENABLED"] == "true"
    assert values["BROKER_ADAPTER_SWITCH_POSITION_OVERRIDE_ENABLED"] == "false"
    assert values["VITE_BROKER_ADAPTER_SWITCH_UI_ENABLED"] == "true"
    assert values["ALLOW_ANONYMOUS_ACCESS"] == "false"
    assert stat.S_IMODE((root / ".env").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "middle" / ".env").stat().st_mode) == 0o600

    log = docker_log.read_text(encoding="utf-8")
    assert ("--profile ib up -d" in log) is ("ibkr_paper" in enabled)
    assert (values["BROKER_ASSET_CAPABILITY_GATE_ENABLED"] == "true") is (
        "alpaca_paper" in enabled
    )
    if "alpaca_paper" in enabled:
        assert values["BROKER_RUNNER_IMAGE"].startswith("ati-local/broker-runner:")
    else:
        assert values["BROKER_RUNNER_IMAGE"].startswith(
            "ghcr.io/winglight/algo-trader/broker-runner-service:"
        )
    combined_output = result.stdout + result.stderr + log
    for secret in SECRET_VALUES.values():
        assert secret not in combined_output


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--enabled-adapters", "sim,sim", "--initial-adapter", "sim"],
        ["--enabled-adapters", "sim,unknown", "--initial-adapter", "sim"],
        ["--enabled-adapters", "sim", "--initial-adapter", "alpaca_paper"],
        ["--enabled-adapters", "sim", "--initial-adapter", "sim", "--alpaca-data-feed", "live"],
        ["--alpaca-base-url", "https://api.alpaca.markets"],
        ["--alpaca-secret-key", "plaintext-forbidden"],
    ],
)
def test_invalid_profile_feed_live_url_and_plaintext_are_rejected(
    runtime: tuple[Path, dict[str, str], Path], extra_args: list[str]
) -> None:
    root, env, _ = runtime
    files = _secret_files(root, {"redis", "mariadb", "admin"})
    result = _run(root, env, _base_args(files) + extra_args)
    assert result.returncode != 0
    assert not (root / ".env").exists()


def test_missing_adapter_credentials_are_rejected(
    runtime: tuple[Path, dict[str, str], Path]
) -> None:
    root, env, _ = runtime
    files = _secret_files(root, {"redis", "mariadb", "admin"})
    for enabled, initial in (
        ("sim,ibkr_paper", "ibkr_paper"),
        ("sim,alpaca_paper", "alpaca_paper"),
    ):
        result = _run(
            root,
            env,
            _base_args(files)
            + ["--enabled-adapters", enabled, "--initial-adapter", initial],
        )
        assert result.returncode != 0
        assert not (root / ".env").exists()


@pytest.mark.parametrize("kind", ["empty", "multiline", "broad", "missing", "symlink"])
def test_secret_file_validation(
    runtime: tuple[Path, dict[str, str], Path], kind: str
) -> None:
    root, env, _ = runtime
    files = _secret_files(root, {"redis", "mariadb", "admin"})
    target = files["admin"]
    if kind == "empty":
        target.write_text("", encoding="utf-8")
    elif kind == "multiline":
        target.write_text("one\ntwo\n", encoding="utf-8")
    elif kind == "broad":
        target.chmod(0o644)
    elif kind == "missing":
        target.unlink()
    elif kind == "symlink":
        real = target.with_name("admin-real")
        target.rename(real)
        target.symlink_to(real)
    result = _run(
        root,
        env,
        _base_args(files) + ["--enabled-adapters", "sim", "--initial-adapter", "sim"],
    )
    assert result.returncode != 0
    assert not (root / ".env").exists()


@pytest.mark.parametrize(
    ("failure_flag", "expected"),
    [
        ("FAKE_FAIL_COMPOSE", ""),
        ("FAKE_FAIL_ENTRYPOINT", ""),
        ("FAKE_BAD_CHECKSUM", "checksum"),
    ],
)
def test_candidate_compose_entrypoint_and_checksum_fail_closed(
    runtime: tuple[Path, dict[str, str], Path], failure_flag: str, expected: str
) -> None:
    root, env, _ = runtime
    files = _secret_files(root)
    failed_env = dict(env)
    failed_env[failure_flag] = "1"
    result = _run(
        root,
        failed_env,
        _base_args(files) + _adapter_args("sim,alpaca_paper", "alpaca_paper", files),
    )
    assert result.returncode != 0
    assert expected in (result.stdout + result.stderr).lower()
    assert not (root / ".env").exists()


def test_cancel_makes_no_changes(runtime: tuple[Path, dict[str, str], Path]) -> None:
    root, env, docker_log = runtime
    result = _run(root, env, [], input_text="\n\n\nn\nn\n1\nn\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Installation cancelled" in result.stdout
    assert not (root / ".env").exists()
    assert docker_log.read_text(encoding="utf-8").splitlines() == ["compose version", "info"]


def test_repeat_remove_add_and_existing_contract_defaults(
    runtime: tuple[Path, dict[str, str], Path]
) -> None:
    root, env, _ = runtime
    files = _secret_files(root)
    first = _run(
        root,
        env,
        _base_args(files) + _adapter_args("sim,alpaca_paper", "alpaca_paper", files),
    )
    assert first.returncode == 0, first.stdout + first.stderr

    second = _run(
        root,
        env,
        ["--non-interactive", "--enabled-adapters", "sim", "--initial-adapter", "sim"],
    )
    assert second.returncode == 0, second.stdout + second.stderr
    after_remove = _read_env(root / ".env")
    assert after_remove["BROKER_RUNNER_ENABLED_ADAPTERS"] == "sim"
    assert after_remove["BROKER_RUNNER_ALPACA_API_KEY_ID"] == SECRET_VALUES["alpaca_key"]
    assert after_remove["BROKER_RUNNER_ALPACA_SECRET_KEY"] == SECRET_VALUES["alpaca_secret"]

    third = _run(
        root,
        env,
        ["--non-interactive"]
        + _adapter_args("sim,ibkr_paper", "ibkr_paper", files),
    )
    assert third.returncode == 0, third.stdout + third.stderr
    after_add = _read_env(root / ".env")
    assert after_add["BROKER_RUNNER_ENABLED_ADAPTERS"] == "sim,ibkr_paper"
    assert after_add["BROKER_RUNNER_DEFAULT_ADAPTER_ID"] == "ibkr_paper"

    fourth = _run(root, env, ["--non-interactive"])
    assert fourth.returncode == 0, fourth.stdout + fourth.stderr
    assert _read_env(root / ".env")["BROKER_RUNNER_DEFAULT_ADAPTER_ID"] == "ibkr_paper"


def test_legacy_ib_upgrade_is_deterministic(
    runtime: tuple[Path, dict[str, str], Path]
) -> None:
    root, env, _ = runtime
    root_env = (root / ".env.example").read_text(encoding="utf-8")
    root_env += "\nBROKER_ADAPTER_MODE=ib\n"
    root_env = root_env.replace("ADMIN_PASSWORD=change_me", "ADMIN_PASSWORD=existing-admin")
    root_env = "\n".join(
        line
        for line in root_env.splitlines()
        if not line.startswith("BROKER_RUNNER_ENABLED_ADAPTERS=")
        and not line.startswith("BROKER_RUNNER_DEFAULT_ADAPTER_ID=")
    ) + "\n"
    (root / ".env").write_text(root_env, encoding="utf-8")
    middle_env = (root / "middle" / ".env.example").read_text(encoding="utf-8")
    replacements = {
        "REDIS_PASSWORD=change-this-even-stronger": "REDIS_PASSWORD=existing-redis",
        "MARIADB_PASSWORD=ChangeThisUserPassword": "MARIADB_PASSWORD=existing-mariadb",
        "TWS_USERID=": "TWS_USERID=existing-ib-user",
        "TWS_PASSWORD=": "TWS_PASSWORD=existing-ib-password",
        "VNC_SERVER_PASSWORD=": "VNC_SERVER_PASSWORD=existing-vnc",
    }
    for old, new in replacements.items():
        middle_env = middle_env.replace(old, new)
    (root / "middle" / ".env").write_text(middle_env, encoding="utf-8")
    result = _run(root, env, [], input_text="\n\n\n\n\n\n\n\n\ny\n")
    assert result.returncode == 0, result.stdout + result.stderr
    values = _read_env(root / ".env")
    assert values["BROKER_RUNNER_ENABLED_ADAPTERS"] == "sim,ibkr_paper"
    assert values["BROKER_RUNNER_DEFAULT_ADAPTER_ID"] == "ibkr_paper"


def test_final_start_failure_restores_both_envs_and_middle_profile(
    runtime: tuple[Path, dict[str, str], Path]
) -> None:
    root, env, docker_log = runtime
    files = _secret_files(root)
    initial = _run(
        root,
        env,
        _base_args(files) + ["--enabled-adapters", "sim", "--initial-adapter", "sim"],
    )
    assert initial.returncode == 0, initial.stdout + initial.stderr
    before_root = (root / ".env").read_bytes()
    before_middle = (root / "middle" / ".env").read_bytes()
    failed_env = dict(env)
    failed_env["FAKE_FAIL_APP_UP"] = "1"
    failed = _run(
        root,
        failed_env,
        _adapter_args("sim,ibkr_paper,alpaca_paper", "alpaca_paper", files)
        + ["--non-interactive"],
    )
    assert failed.returncode == 42
    assert (root / ".env").read_bytes() == before_root
    assert (root / "middle" / ".env").read_bytes() == before_middle
    log = docker_log.read_text(encoding="utf-8")
    assert "--profile ib stop ib-gateway" in log


def test_install_wrapper_forwards_setup_arguments_and_guards_updates() -> None:
    source = (PUBLIC_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert 'SETUP_ARGS=("$@")' in source
    assert 'exec bash ./setup_and_run.sh "${SETUP_ARGS[@]}"' in source
    assert "Non-interactive update requires ATI_ALLOW_UPDATE=1" in source


def test_compose_does_not_pretend_vite_flags_are_runtime_configuration() -> None:
    source = (PUBLIC_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    frontend = source.split("\n  frontend:\n", 1)[1].split("\nnetworks:\n", 1)[0]
    assert "VITE_" not in frontend


def test_alpaca_wheel_lock_sbom_and_import_smoke_contract() -> None:
    lock_path = PUBLIC_ROOT / "docker" / "alpaca-runtime-wheels.lock"
    rows = [
        line.split("|")
        for line in lock_path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert len(rows) == 8
    assert all(len(row) == 6 for row in rows)
    assert {row[0] for row in rows} == {"any", "amd64", "arm64"}
    assert sum(row[1] == "pandas" and row[0] == "amd64" for row in rows) == 1
    assert sum(row[1] == "pandas" and row[0] == "arm64" for row in rows) == 1
    assert all("api.alpaca.markets" not in row[5] for row in rows)

    sbom = json.loads(
        (PUBLIC_ROOT / "docker" / "alpaca-paper.spdx.json").read_text(encoding="utf-8")
    )
    sbom_hashes = {
        checksum["checksumValue"]
        for package in sbom["packages"]
        for checksum in package.get("checksums", [])
    }
    assert {row[4] for row in rows}.issubset(sbom_hashes)
    assert ADAPTER_ARCHIVE_SHA256 in sbom_hashes

    dockerfile = (PUBLIC_ROOT / "docker" / "Dockerfile.broker_runner_adapters").read_text(
        encoding="utf-8"
    )
    assert "sha256sum -c SHA256SUMS" in dockerfile
    assert "python -m pip check" in dockerfile
    assert "from alpaca.data.historical import StockHistoricalDataClient" in dockerfile
    assert "from alpaca.trading.client import TradingClient" in dockerfile
