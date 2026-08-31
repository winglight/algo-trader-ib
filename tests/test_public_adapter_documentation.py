from pathlib import Path


def test_readmes_match_public_adapter_distribution_boundaries() -> None:
    english = Path("README.md").read_text(encoding="utf-8")
    chinese = Path("README_cn.md").read_text(encoding="utf-8")
    installer = Path("setup_and_run.sh").read_text(encoding="utf-8")

    for adapter_id in ("sim", "ibkr_paper", "alpaca_paper", "ccxt_crypto", "projectx_topstep"):
        assert adapter_id in english
        assert adapter_id in chinese
        assert adapter_id in installer

    assert "OKX Demo Spot and USDT perpetual" in english
    assert "OKX Demo Spot 与 USDT 永续" in chinese
    assert "one guarded `ccxt_crypto` profile" in english
    assert "统一的受控 `ccxt_crypto` profile" in chinese
    assert "ProjectX/Topstep is enabled: choose local `dry_run` or provider `read_only`" in english
    assert "如果启用 ProjectX/Topstep：选择本地 `dry_run` 或 provider `read_only`" in chinese
    assert "rejects `provider_api` mode" in english
    assert "并拒绝 `provider_api`" in chinese


def test_public_installer_configures_the_unified_okx_demo_profile() -> None:
    env_example = Path(".env.example").read_text(encoding="utf-8")
    installer = Path("setup_and_run.sh").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    for key in (
        "BROKER_RUNNER_CCXT_CRYPTO_PERPETUAL_ALLOWED_SYMBOLS=BTC/USDT:USDT,ETH/USDT:USDT",
        "BROKER_RUNNER_CCXT_CRYPTO_PERPETUAL_EXECUTION_TARGET_ID=okx-perpetual-demo-paper-1",
        "BROKER_RUNNER_CCXT_CRYPTO_PERPETUAL_MARKET_DATA_TARGET_ID=okx-perpetual-demo-market-1",
        "BROKER_RUNNER_CCXT_CRYPTO_PERPETUAL_POSITION_MODE=ONE_WAY",
        "BROKER_RUNNER_CCXT_CRYPTO_PERPETUAL_MARGIN_MODE=ISOLATED",
        "BROKER_RUNNER_CCXT_CRYPTO_PERPETUAL_FIXED_LEVERAGE=2",
    ):
        assert key in env_example

    assert "BROKER_RUNNER_CCXT_CRYPTO_ALLOWED_SYMBOLS BTC/USDT,ETH/USDT" in installer
    assert "BROKER_RUNNER_CCXT_CRYPTO_EXECUTION_TARGET_ID okx-spot-demo-paper-1" in installer
    assert "BROKER_RUNNER_CCXT_CRYPTO_PERPETUAL_ALLOWED_SYMBOLS BTC/USDT:USDT,ETH/USDT:USDT" in installer
    assert "CRYPTO_SPOT\", \"CRYPTO_PERPETUAL" in installer
    assert "BROKER_RUNNER_CCXT_CRYPTO_PERPETUAL_ALLOWED_SYMBOLS:" in compose
    for obsolete in (
        "BROKER_RUNNER_CCXT_CRYPTO_PUBLIC_DATA_ENABLED",
        "BROKER_RUNNER_CCXT_CRYPTO_PRIVATE_READ_ENABLED",
        "BROKER_RUNNER_CCXT_CRYPTO_TRADING_ENABLED",
        "BROKER_RUNNER_CCXT_CRYPTO_MARKET_ORDER_ENABLED",
    ):
        assert obsolete not in env_example
        assert obsolete not in installer
        assert obsolete not in compose


def test_public_installer_collects_okx_demo_credentials_without_plaintext_cli() -> None:
    installer = Path("setup_and_run.sh").read_text(encoding="utf-8")

    for option in (
        "--okx-api-key-file",
        "--okx-secret-key-file",
        "--okx-passphrase-file",
    ):
        assert option in installer
    assert 'prompt_masked_value "OKX Demo API key" "$CURRENT_OKX_KEY"' in installer
    assert 'resolve_secret "$CURRENT_OKX_SECRET"' in installer
    assert 'resolve_secret "$CURRENT_OKX_PASSPHRASE"' in installer
    assert 'env_set_quoted "$ROOT_CANDIDATE" BROKER_RUNNER_CCXT_CRYPTO_API_KEY "$OKX_KEY"' in installer
    assert "--okx-api-key|--okx-secret-key|--okx-passphrase" in installer


def test_public_installer_exposes_only_safe_projectx_modes() -> None:
    env_example = Path(".env.example").read_text(encoding="utf-8")
    installer = Path("setup_and_run.sh").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    for option in (
        "--projectx-mode dry_run|read_only",
        "--projectx-api-base-url URL",
        "--projectx-username-file PATH",
        "--projectx-api-key-file PATH",
        "--projectx-account-file PATH",
    ):
        assert option in installer
    for key in (
        "BROKER_RUNNER_PROJECTX_EXECUTION_MODE=dry_run",
        "BROKER_RUNNER_PROJECTX_MARKET_DATA_MODE=local",
        "BROKER_RUNNER_PROJECTX_LIVE=false",
        "BROKER_RUNNER_PROJECTX_PROVIDER_API_ACTIVATION_ENABLED=false",
        "BROKER_RUNNER_PROJECTX_LOCAL_PERSONAL_DEVICE_ATTESTED=false",
        "BROKER_RUNNER_PROJECTX_REMOTE_EXECUTION=false",
    ):
        assert key in env_example
    for key in (
        "BROKER_RUNNER_PROJECTX_EXECUTION_MODE:",
        "BROKER_RUNNER_PROJECTX_MARKET_DATA_MODE:",
        "BROKER_RUNNER_PROJECTX_LIVE:",
        "BROKER_RUNNER_PROJECTX_PROVIDER_API_ACTIVATION_ENABLED:",
        "BROKER_RUNNER_PROJECTX_REMOTE_EXECUTION:",
    ):
        assert key in compose
    assert 'provider_api is not available through the public installer' in installer
    assert '--projectx-username|--projectx-api-key|--projectx-account' in installer
    assert 'projectx.get("execution_mode") in {"dry_run", "read_only"}' in installer
