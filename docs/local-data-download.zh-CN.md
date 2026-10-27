# 本地 K 线下载
执行边界：下载的 Parquet 是原生 reader 兼容格式，但现有 Cloud 派发本地 connector 接收签名 dataChunks 并准备 SQLite 输入。在 Studio 选择本地 worker 并不自动换用这些文件；须核对实际 reader 路径，不替换冻结输入，不冒称本地文件已用于计算。

安装器不下载行情。LTB 页面只读取本地覆盖并提供脚本/文档下载，不收密钥、不执行命令。

## 在已安装研究镜像中运行
在公共安装目录运行（Docker Compose v2）：
```sh
./scripts/download_market_data.sh --provider alpaca --symbols SPY,QQQ --timeframes 1m,5m --start 2025-01-01T00:00:00Z --end 2025-02-01T00:00:00Z
```
默认仅显示计划；你明确加 `--execute` 才下载。交互终端可省略 symbols/timeframes，由脚本提示输入列表。`--status` 只查看已有数据。三份 download_*.py 是同一共享 SDK CLI 的提供商启动器，需 ATI 已安装的 Python/SDK 环境，不是假称无依赖的独立程序。也可在已有授权 Python 环境安装 ati-shared-sdk[local-data] 后运行。

凭据仅在你本地环境配置，不写到网页、命令行参数、公开文件、截图或任务卡；公共 shell 只转发文档明确的提供商环境变量。权限错误不通过 sudo/chmod 全盘解决。

## 三种方式
* IB：用户已登录的 TWS/IB Gateway，开通历史行情权限。脚本不收账户密码或 API key。指定 `--ib-host`、`--ib-port`、独立 `--ib-client-id`；`--ib-contracts /path/contracts.json` 给每个 symbol 明确的 `{ "conId": 123, "exchange": "SMART" }`（123只是格式示例，不是推荐合约）。设置 ATI_IB_CONTRACTS_FILE 指向该无秘密本地文件，公共 shell 会将其只读挂入容器。脚本仅调用 API 握手、合约确认/历史行情，不调用订单/账户方法。建议在 Gateway 启用 API 只读设置；连接握手本身不等于具备只读权限强制保护。支持1m/5m/15m/1h；不猜 IB 日线日期的时区/交易日映射，不拼接不同到期期货为连续合约。权限、限速、过期期货历史限制仍有效。
* Alpaca：本地配置 APCA_API_KEY_ID、APCA_API_SECRET_KEY；仅美股/ETF，raw 未调整，`--feed iex|sip` 按实际权限。支持1m/5m/15m/1h/1d。完整翻页；不把期货当股票，不自动购买订阅。
* Databento：本地 DATABENTO_API_KEY；明确 `--dataset` 与真实 raw instrument symbol。支持1m/1h/1d，不自动合成连续期货。`--execute --max-cost 5` 会先获得全部报价，再由终端确认或明确 `--confirm-cost`。max-cost 是报价上限而非账单绝对封顶，供应商按实际交付字节计费。拒绝/超额不下载，不自动重试收费下载。

## 文件与解释
容器默认 `/app/data/market-data`，宿主默认 `./data/market-data`（可设 ATI_MARKET_DATA_DIR）。研究运行只读；仅用户明确下载命令读写同一目录。文件为 bar_<timeframe>/<symbol>/YYYYMM_<symbol>_<timeframe>.parquet，原生UTC timestamp及OHLCV，按月原子替换、按时间戳去重，不删除旧分片。来源/合约/feed不同或旧文件来源未知时拒绝混入，请使用独立数据目录并明确选择研究数据源；辅助来源文件不是回测登记门槛。
开始时间含、结束时间不含，均须带时区。写入成功只表示实际行已保存，不代表连续/完整交易日；空数据报错，后续失败保留之前数据。界面显示真实时间戳范围和读取异常，不用文件名推整月完整。节省本地对应云计算 credits 不等于第三方 AI/数据/订阅免费。


Public installation: run `bash scripts/download_market_data.sh` after installing local offline tools. Set keys only in your local shell, never in the webpage. Optional `ATI_IB_CONTRACTS_FILE` names a local exact-contract JSON mounted read-only; Gateway remains user-managed.
