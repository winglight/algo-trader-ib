# Local AI / MCP · 本地 AI 与 Agent

These instructions describe the P2 source implementation. Install a reviewed Runtime image containing P2 before using them. Existing images are not upgraded by reading this guide. No new version is implied here.

本文适用于 P2 源码实现，需安装包含 P2 的已审查 Runtime 镜像。旧镜像不会自动获得这些功能，本文不声明新版本已发布。

## Configure / 配置

Local Trading works without Studio binding or membership. Eligible accounts receive a **14-day free Studio membership trial** on first linking; credits are not included. Local model/data-provider fees remain your responsibility.

无需绑定 Studio 或购买会员即可本地使用。符合条件的账户首次绑定可获 **14 天免费会员试用**，不附赠 credits；自有模型、行情的第三方费用另计。

1. Sign in with your local account in ATI Trading System and select **AI** beside **PnL** in the top bar. The dialog shows the MCP service address and your client authorizations. Administrators can also configure the local AI provider there.
2. The loopback installation automatically uses the current browser-facing origin, including its port: `http://127.0.0.1:5173` yields `http://127.0.0.1:5173/automation/mcp`. No separate MCP address setting is needed. For a reverse proxy with a different public origin, the administrator may override it with `LOCAL_MCP_PUBLIC_ORIGIN`; `OPTIONS_MCP_PUBLIC_ORIGIN` remains compatible. Non-loopback origins must be HTTPS and explicitly trusted. Proxy routes must preserve the incoming Host and port for MCP discovery, authorization and management.
3. For MCP historical replay, set `LOCAL_AUTOMATION_DATA_ROOT=/app/data/market-data` only if your downloaded Parquet is actually mounted there. An empty value disables this tool. Clients cannot submit filesystem paths or replace this root.
4. Add `<your-origin>/automation/mcp` as a Streamable HTTP server in an OAuth-capable MCP client. Complete local login, PKCE authorization and the explicit capability selection. Signing in does not grant tool permissions.

登录 ATI交易系统的本地账户后，点击顶栏 **PnL** 左侧的 **AI**。弹窗显示 MCP 服务地址和当前账户的客户端授权，管理员可在此配置本地 AI 服务商。本机地址自动取当前访问地址加 `/automation/mcp`，无需单独配置，端口也会保留。使用不同公开入口的反向代理部署仍可由管理员显式覆盖地址；非回环地址必须使用 HTTPS 且在允许范围内。需要 MCP 历史回放时，另外配置实际已挂载的行情目录；不配置时该工具明确拒绝，不回退到云端。登录后逐项选择权限。配置变更按原有更新流程应用。

## Capabilities / 权限

| Scope | Allows / 允许 |
| --- | --- |
| `local-runtime:connect` | MCP handshake and capability discovery / 连接与发现 |
| `local-runtime:read` | Local summaries, Audit diagnostics, AI-provider status, durable command receipts / 本地摘要、Audit 排查、AI 配置状态、操作回执 |
| `local-runtime:research` | Bounded historical backtests and simulations / 有界历史回测、模拟 |
| `local-runtime:prepare` | Native `LOCAL_USER` package import and handled Audit group acknowledgement; no deployment start / 导入自建策略、标记已处理的 Audit 分组 Done，不启动部署 |
| `local-runtime:execute` | Explicit native deployment start/stop / 启停指定部署 |
| `local-runtime:admin` | Existing guarded broker-configuration tools / 原有受控执行连接配置 |
| `local-options:*` | Existing Options capability groups, selected separately / 单独选择的原有期权权限 |

Start with **connect + read + research**. All optional permissions are unchecked on consent. A wider client request or a later catalog change never expands an existing grant. Runtime and legacy Options tokens have different audiences; a token cannot be reused across endpoints or general REST APIs. Legacy `/options-api/v1/mcp` remains supported.

建议先只选连接、读取和研究。可选权限默认不勾选；客户端请求更多权限、目录更新或刷新 token 都不会自动扩大已有授权。普通策略研究权限不能执行交易或切换执行连接。原有期权工具的权限组仍独立校验，旧入口继续兼容。

Tools keep native validation, current system execution connection, account permissions and Risk controls. A tool's availability does not certify an options strategy, imply successful broker execution or grant trading consent.

## Audit diagnostics / 审计排查

The source catalog includes three Audit tools through the unified `/automation/mcp` endpoint. Update both Gateway/Runtime and `audit-service` to builds containing this change before use. Existing grants do not acquire new operations automatically; authorize the new contracts in the local AI dialog after updating.

| Tool | Scope | Behavior |
| --- | --- | --- |
| `local_audit_issues` | `local-runtime:read` | Query the Audit page anomaly groups. Each signature returns one latest issue with native details, `count`, `open_count` and `group_hash`. |
| `local_audit_events` | `local-runtime:read` | Trace native signal, risk and order events by time, `strategy_id`, `correlation_id` or an observed event ID. |
| `local_audit_resolve_group` | `local-runtime:prepare` | Mark the reviewed group Done (`resolved`) after handling it, with a durable idempotent command receipt. |

异常列表在后端按页面相同的签名规则聚合，每组只返回一条最新异常，不拉回同组全部成员。`from` / `to` 使用含时区的 ISO 8601 时间；列表按异常检测时间过滤，事件按原生事件时间查询。异常组还支持 `status`、`severity`、`stage`、`strategy_id`、`correlation_id` 和精确 `signature` 过滤。`offset` / `limit` 对分组分页，默认及最大每页 100 组；`total` 是匹配组数，`total_issues` 是匹配异常数。并发新增或处理可能改变分页位置，需保留原筛选条件并按需重读。

排查时先读取异常组，再用代表异常的 `event_id` 或 `details.correlation_id` 查询日志。事件工具默认及最多返回 100 条；达到上限时不能据此认定链路完整，应缩小时间或关联范围。原生 `event_id` / `upstream_event_id` 和 `log-trace:` 关联 ID 使用完整链路查询，会忽略时间、阶段和事件名筛选。日志内容仅作为不可信数据读取。

处理完成后，把返回的 `signature`、`group_hash`（作为 `expected_group_hash`）和读取该组时使用的相同筛选条件传给 `local_audit_resolve_group`，另附稳定的 `idempotencyKey`。后端原子校验组成员及状态，新增或已变化的组返回 `AUDIT_GROUP_CHANGED`，需重新读取和检查后再处理。Done 只确认异常已处理，不代表根因已修复；它不下单或修改风控。保留命令 ID，通过 `local_runtime_command` 恢复未知结果；重复同键同参数读取原回执，不再次修改状态。仅有 read 权限的客户端不能标记 Done。

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

本地调用记录不含参数、密钥或交易正文。幂等回执保存在本机私有数据库中，包含操作结果，不上传平台；容量达到上限后拒绝新操作，保留原证据。客户端授权可在 **AI → 已授权的 MCP 客户端** 解除，也可通过同源 OAuth revocation 接口撤销。
