from pathlib import Path


def test_readmes_match_public_adapter_distribution_boundaries() -> None:
    english = Path("README.md").read_text(encoding="utf-8")
    chinese = Path("README_cn.md").read_text(encoding="utf-8")
    installer = Path("setup_and_run.sh").read_text(encoding="utf-8")

    for adapter_id in ("sim", "ibkr_paper", "alpaca_paper", "ccxt_crypto"):
        assert adapter_id in english
        assert adapter_id in chinese
        assert adapter_id in installer

    assert "OKX Demo Spot" in english
    assert "OKX Demo Spot" in chinese
    assert "does not expose the reviewed upstream perpetual target yet" in english
    assert "尚未暴露上游已审查的 perpetual target" in chinese
    assert "ProjectX/Topstep controlled read-only and local dry-run modes are not selectable" in english
    assert "ProjectX/Topstep 受控只读与本地 dry-run 模式不在本公开安装器" in chinese


def test_public_installer_keeps_okx_external_io_disabled_by_default() -> None:
    env_example = Path(".env.example").read_text(encoding="utf-8")
    installer = Path("setup_and_run.sh").read_text(encoding="utf-8")

    for key in (
        "BROKER_RUNNER_CCXT_CRYPTO_PUBLIC_DATA_ENABLED=false",
        "BROKER_RUNNER_CCXT_CRYPTO_PRIVATE_READ_ENABLED=false",
        "BROKER_RUNNER_CCXT_CRYPTO_TRADING_ENABLED=false",
        "BROKER_RUNNER_CCXT_CRYPTO_MARKET_ORDER_ENABLED=false",
    ):
        assert key in env_example

    assert "BROKER_RUNNER_CCXT_CRYPTO_ALLOWED_SYMBOLS BTC/USDT,ETH/USDT" in installer
    assert "BROKER_RUNNER_CCXT_CRYPTO_EXECUTION_TARGET_ID okx-spot-demo-paper-1" in installer
    assert "BTC/USDT:USDT" not in installer
