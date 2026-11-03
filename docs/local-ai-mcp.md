# Local AI / MCP · 本地 AI 与 Agent

These instructions describe the P2 source implementation. Install a reviewed Runtime image containing P2 before using them. Existing images are not upgraded by reading this guide. No new version is implied here.

本文适用于 P2 源码实现，需安装包含 P2 的已审查 Runtime 镜像。旧镜像不会自动获得这些功能，本文不声明新版本已发布。

## Configure / 配置

Local Trading works without Studio binding or membership. Eligible accounts receive a **14-day free Studio membership trial** on first linking; credits are not included. Local model/data-provider fees remain your responsibility.

无需绑定 Studio 或购买会员即可本地使用。符合条件的账户首次绑定可获 **14 天免费会员试用**，不附赠 credits；自有模型、行情的第三方费用另计。

1. Sign in with your existing local Trading account; open **AI / MCP** in the top bar. The same page provides local AI configuration, tool capabilities, authorized clients and activity summaries.
2. In the installation `.env`, set `LOCAL_MCP_PUBLIC_ORIGIN` to the exact browser-facing origin, for example `http://127.0.0.1:5173` for the default loopback installation. Use HTTPS for non-loopback access. Existing `OPTIONS_MCP_PUBLIC_ORIGIN` is accepted if the new setting is empty. Apply configuration through your normal Runtime update/recreate procedure; this guide does not restart a running stack.
3. For MCP historical replay, set `LOCAL_AUTOMATION_DATA_ROOT=/app/data/market-data` only if your downloaded Parquet is actually mounted there. An empty value disables this tool. Clients cannot submit filesystem paths or replace this root.
4. Add `<your-origin>/automation/mcp` as a Streamable HTTP server in an OAuth-capable MCP client. Complete local login, PKCE authorization and the explicit capability selection. Signing in does not grant tool permissions.

登录本地账户后，从顶部 **AI / MCP** 进入统一页面。管理员在安装目录 `.env` 设置准确的浏览器访问地址，非回环地址必须使用 HTTPS。需要 MCP 历史回放时，另外配置实际已挂载的行情目录；不配置时该工具明确拒绝，不回退到云端。客户端使用 `<访问地址>/automation/mcp`，登录后逐项选择权限。配置变更按原有更新流程应用，本指南不会自动重启服务。

## Capabilities / 权限

| Scope | Allows / 允许 |
| --- | --- |
| `local-runtime:connect` | MCP handshake and capability discovery / 连接与发现 |
| `local-runtime:read` | Local summaries, AI-provider status, durable command receipts / 本地摘要、AI 配置状态、操作回执 |
| `local-runtime:research` | Bounded historical backtests and simulations / 有界历史回测、模拟 |
| `local-runtime:prepare` | Native `LOCAL_USER` package import; no deployment start / 导入自建策略，不启动部署 |
| `local-runtime:execute` | Explicit native deployment start/stop / 启停指定部署 |
| `local-runtime:admin` | Existing guarded broker-configuration tools / 原有受控执行连接配置 |
| `local-options:*` | Existing Options capability groups, selected separately / 单独选择的原有期权权限 |

Start with **connect + read + research**. All optional permissions are unchecked on consent. A wider client request or a later catalog change never expands an existing grant. Runtime and legacy Options tokens have different audiences; a token cannot be reused across endpoints or general REST APIs. Legacy `/options-api/v1/mcp` remains supported.

建议先只选连接、读取和研究。可选权限默认不勾选；客户端请求更多权限、目录更新或刷新 token 都不会自动扩大已有授权。普通策略研究权限不能执行交易或切换执行连接。原有期权工具的权限组仍独立校验，旧入口继续兼容。

Tools keep native validation, current system execution connection, account permissions and Risk controls. A tool's availability does not certify an options strategy, imply successful broker execution or grant trading consent.

## stdio clients / stdio 客户端

Use the Python bridge from the Runtime source, with Python 3.11+ and `httpx` installed:

```sh
python scripts/run/mcp/options_stdio.py \
  --authorize --origin http://127.0.0.1:5173 \
  --credentials "$HOME/.ati-local-mcp.json"

python scripts/run/mcp/options_stdio.py \
  --credentials "$HOME/.ati-local-mcp.json"
```

The first command opens the local OAuth consent page and uses a temporary loopback PKCE callback. The second is the MCP client's stdio command. The filename is retained for compatibility; the default resource is the unified Runtime MCP. Default client scopes are connect, read and research. For broader authority, use a **new credential file** and pass each explicit `--scope`; review the local consent page. Never put tokens or passwords in an MCP configuration or a repository.

凭据文件为当前用户独占的 `0600` 文件，包含 refresh secret；桥接器拒绝公共可读文件、软链接及错误目标地址。它不读取 `.env`，不会自动使用管理员密码登录，也不会注入绕过权限检查的额外工具。旧的无参数、自动管理员登录方式已移除；旧客户端需重新授权。轮换使用进程锁和原子写入，多个桥接进程不会重用已消费的 refresh token。

## Recovery and evidence / 恢复与证据

`local_runtime_import`, `local_runtime_start`, `local_runtime_stop` and `local_runtime_replay` require an `idempotencyKey`. Reusing the same key with identical arguments returns the same receipt; different arguments are rejected. Use `local_runtime_command` with the returned real ID to read status. After a transport failure, read the native state/receipt before creating another key. A process interruption is `outcome_unknown`, never a reason for automatic replay. Accepted operations are not rolled back by revoking a client.

普通策略的导入、启停和回放使用幂等键。重复同键同参数恢复原回执，异参拒绝。未知结果先查询原生状态，不自动重试业务命令。撤销客户端只终止后续授权，不回滚已经受理的操作。期权工具继续使用原有原生操作 ID 和幂等约定。

MCP replay uses the same native kernel and restrictions as [offline replay](local-user-replay.md). Results remain `LOCAL_USER`, `platformCertified: false`, with no Cloud Audit receipt. The Gateway permits two concurrent replay jobs per process, a maximum 366-day context-plus-score window, at most 128 MiB of candidate Parquet files, a 120-second wall budget and a 16 MiB report. Linux children also have a 4 GiB address-space limit. Unsupported ML/stateful/multi-asset inputs fail explicitly. Larger native research belongs in the existing installed research runner; this bounded tool is not a substitute for its task/data contracts.

Activity stores only tool name, status, server-generated request ID and time, paginated on demand, with up to 5,000 summaries. Arguments, tokens and trade payloads are excluded from activity. Local durable command receipts do retain operation results in the private local browser-session database; they are not uploaded. There are up to 2,000 command receipts and a 256 MiB admission threshold; reaching capacity rejects new commands without deleting prior evidence. Back up local state before maintenance.

本地调用记录不含参数、密钥或交易正文。幂等回执保存在本机私有数据库中，包含操作结果，不上传平台；容量达到上限后拒绝新操作，保留原证据。主页面按需分页读取，不后台轮询。客户端可在 **AI / MCP → 已授权客户端** 撤销，或通过同源 OAuth revocation 接口撤销。
