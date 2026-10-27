# Offline local replay / 离线本地回放

**LOCAL_USER only, not a certified Cloud Audit, Study, or RESEARCH_COMPUTE job.**
This CLI consumes your actual local Parquet via `HistoricalDataProvider` and the existing `HistoricalReplayRunner`/shared economics kernel. It never downloads data, calls a broker, trains ML, replaces signed Cloud packets or changes strategy deployment state.

**仅本地用户来源，不是平台认证报告。** 此入口直接读取本地 Parquet 并复用现有原生历史回放核心；不连接行情商/交易服务、不下单、不训练 ML，不替换 Cloud 派发的签名输入。Studio 本地 connector 的完整 RESEARCH_COMPUTE 封装是另一条尚未完成的集成路径，不能将此 CLI 当其已交付。

## Required explicit contract / 明确合同
Inside the SDK-enabled Python environment (or dedicated local SDK image), supply your workflow, execution and instrument JSON. Dates must have timezones. Start is inclusive, end exclusive; `context-start` is a fixed causal source origin, not a rolling warmup shortcut.

```sh
python -m ati_shared_sdk.common.historical_runtime.local_cli \
  --workflow /work/workflow.json --execution /work/execution.json \
  --instrument /work/instrument.json --symbol QQQ --timeframe 5m \
  --start 2025-01-02T14:30:00Z --end 2025-01-02T21:00:00Z \
  --context-start 2024-12-31T20:50:00Z \
  --data-root /app/data/market-data --output /output/local-result.json
```

只读挂载输入 JSON 和行情目录，输出目录单独可写；不要挂环境 token、Docker socket 或交易配置。输出完整 JSON 原子建立且不覆盖已有文件。`--batch-size`（1–8192）仅改变经济回放分片，不改变评分窗口；当前向量特征路径保留固定起点的全部因果输入，长窗内存/计算成本需自行评估，不宣称有界内存流式全能力。

Execution must explicitly include `initialCash`, positive integer `defaultQuantity`, `commissionModel: IBKR_DEFAULT`, a real `feeProfileId`, fixed-tick `slippageModel` with ticks/tickSize, `fillModel` and `flattenAtEnd: true`. Risk and EE specs must exist in the workflow. Instrument requires exact symbol, assetClass (`equity`, `etf`, or supported native `future`), USD currency, pointValue and tickSize. Unsupported futures cannot silently become point-value-1 equities. No cost defaults are silently supplied. Explicitly configured zero slippage remains zero and is disclosed, not disguised as real slippage.

费用、滑点、整数数量、Risk/EE 和资产乘数必须明确；不提供空默认“净收益”。现仅支持单一资产/TF、vectorized、bars → indicator_pack / signal_rule_engine → trade_signal_batch，以及原生 next_bar_open/bar_close。stateful workflow、ML、外部上下文、多源/多资产和未支持 Risk 动作会明确拒绝，不降级伪结果。

## Runnable synthetic smoke example / 可运行合成示例
This generates **synthetic test bars**, not market observations or an investment strategy. Use a new directory; existing directories are never overwritten.

```sh
python -m ati_shared_sdk.common.historical_runtime.local_example --directory /work/example
python -m ati_shared_sdk.common.historical_runtime.local_cli \
  --workflow /work/example/workflow.json --execution /work/example/execution.json \
  --instrument /work/example/instrument.json --symbol QQQ --timeframe 5m \
  --start 2025-01-02T14:30:00Z --end 2025-01-02T21:00:00Z \
  --context-start 2024-12-31T20:50:00Z \
  --data-root /work/example/data --output /work/example/result.json
```

示例产生 500 根不评分暖身与 78 根评分 bar；测试还将一次性/多分片结果与现有 prepared 原生执行器逐成交比较。**示例收益不是绩效证据。** 使用真实数据时，下载文档说明的同一原生目录格式可直接读取，无需注册。缺来源 sidecar 仍可运行，但来源标记 UNKNOWN；有 sidecar 只标 present_unverified，不当供应商认证。

## Evidence and failure boundaries / 证据与失败
Result includes actual candidate source-file SHA-256 hashes (both normal and legacy layouts), workflow identity, engine digest/version, scoring window, unscored context count and coverage checks, explicit execution contract and native trade/cost results. Warmup bars never enter the economic ledger or scored denominator. Insufficient causal history, changed source files during read, incorrect symbol/TF, explicit unclosed bars, timezone-less timestamps and symlinks fail visibly.

日期范围仅是 observed bars，不等于权威交易日历完整覆盖；原始数据修订的可获得时间未知会如实标 UNKNOWN。本地输出不带 Cloud permit/审计签名，不自动同步到正式研究结论。离线运行本身不消耗对应云计算 credits，但容器/用户资源、既有数据/模型费用不因此免费。

### Measured scope / 实测规模边界

A synthetic 30,000 scored-bar + 500 unscored-prefix run at batch size 2,048 completed in about 57.4 seconds with process peak RSS about 393 MiB in one development container. This is a diagnostic throughput observation, not a market/performance result or production capacity guarantee. The CLI currently loads the selected window into memory and the vectorized path retains/re-evaluates a growing fixed-origin context; smaller batches may increase repeated feature computation. Multi-year minute data and all hardware sizes are not certified by this test. Source hashes cover the native reader's overlapping file candidates (including unknown-bound files), not unrelated years.

合成 30,000 根评分 + 500 根暖身、每批 2,048 根，在开发容器实测约 57.4 秒、进程峰值内存约 393 MiB。当前会加载完整所选窗口并保留增长的固定起点上下文，非有界内存流式；更小分片可能重复更多特征计算。该结果不代表多年分钟数据性能保证，也不代表策略收益验证。

## Public container wrapper / 公共安装入口
Use `bash scripts/run_local_backtest.sh` with the same CLI arguments above, using host paths. It maps each input JSON and the data directory read-only, and only the output parent directory writable. Network is disabled. No identity credentials, keys, trading sockets, Cloud grant or account directory are mounted. The image is SDK-only; this is not a Studio research worker.
