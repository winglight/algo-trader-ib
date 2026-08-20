# ATI Local Runtime

[English Version](README.md)

ATI Local Runtime 是 Algo Trading Intelligence 的本地运行版。它把交易工作台、API、账户、订单、行情、风控、策略、仿真和策略规格服务打包为一套 Docker Compose 环境，适合在个人电脑或自有服务器上运行。

官方网站：[ati.broyustudio.com](https://ati.broyustudio.com)  
会员与产品页入口：[ati.broyustudio.com](https://ati.broyustudio.com)  
云端 Strategy Studio：[ati-studio.broyustudio.com](https://ati-studio.broyustudio.com)  
本地交易系统 Demo：[ati-trading.broyustudio.com](https://ati-trading.broyustudio.com)

开源 Broker adapters：[winglight/algo-trader-broker-adapters](https://github.com/winglight/algo-trader-broker-adapters)

本地版用于运行和验证自己的交易环境；云端 Studio 用于托管式 workflow 设计、云端试用和后续订阅能力。

首次安装后，本地环境在未绑定云平台用户前可试用 24 小时。绑定云平台用户后，本地服务会按云平台订阅等级下发的能力启用或禁用。

![ATI Local Runtime](images/screenshot.png)

## 功能概览

- 本地交易工作台：浏览器访问 `http://127.0.0.1:5173`。
- Broker adapter 支持 `sim`、`ibkr_paper` 和 `alpaca_paper`；`sim` 始终安装，可同时启用多个 Paper profile。
- 核心服务完整运行：API、account、orders、market data、risk、strategy、simulation、strategy spec。
- 本地策略目录：`strategies/` 会挂载到容器中，便于查看示例和添加自定义策略。
- 数据持久化：`.env`、`middle/.env`、`data/`、`logs/` 都保留在本机；其中云端绑定状态与本机安装身份保存在 `data/license/`，更新镜像、重建容器或清理日志都不会要求重新绑定。
- 默认关闭后端 docs/redoc/openapi，默认不暴露后端、Redis、MariaDB 到宿主机公网端口。

![PnL Calendar](images/pnl-calendar.png)

## 一键安装

复制下面这一行到终端执行：

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/winglight/algo-trader-ib/main/scripts/install.sh)"
```

### 按系统安装

#### Windows（使用 WSL）

安装脚本必须在 WSL 的 Linux 终端中运行，不能直接在 PowerShell 或 CMD 中运行。
如果尚未安装 WSL，请以管理员身份打开 PowerShell，执行：

```powershell
wsl --install
```

按提示重启 Windows，打开已安装的 Linux 发行版（默认通常为 Ubuntu）并完成首次初始化，
然后在该 WSL 终端中执行上面的一键安装命令。脚本会在 WSL 内检查依赖；如果 Docker
尚不可用，会按 Linux 方式自动安装并启动 Docker，期间可能要求输入 WSL 用户的
`sudo` 密码。安装目录默认为 WSL 用户主目录下的 `~/ati-local-runtime`。

#### macOS

建议先从 [Docker 官网](https://www.docker.com/products/docker-desktop/) 手动安装
Docker Desktop，启动应用并等待 Docker Engine 就绪，
再打开“终端”执行上面的一键安装命令。脚本会检查 `docker compose` 和 Docker Engine；
如果尚未安装 Docker，会尝试通过 Homebrew 安装 Docker Desktop，但手动安装更便于
完成首次启动、权限确认和系统设置。安装目录默认为 `~/ati-local-runtime`。

#### Linux

在 Linux 终端中直接执行上面的一键安装命令。脚本检测到 Docker 不可用时，会在受支持的
`apt-get`、`dnf` 或 `yum` 系统上自动安装 Docker Engine、Compose 插件并启动 Docker
服务；安装系统软件时需要 root 权限或 `sudo`。如果安装后提示 Docker 组权限尚未生效，
请执行 `newgrp docker` 或重新登录，再重新运行一键安装命令。安装目录默认为
`~/ati-local-runtime`。

在以上环境中，脚本都会下载最新安装文件、检查运行依赖、交互式生成或保留配置、
拉取所选服务与 adapter 所需镜像/插件，并通过 Docker Compose 创建本地服务。

安装脚本会自动生成 Redis、MariaDB、Web 登录和 IB Gateway VNC 密码，并写入权限为
`0600` 的 `.env` 或 `middle/.env`。已有配置中的非空密码会在重跑或升级时保留。

交互安装只需要选择启用的 adapter 和初始 adapter，并填写所选券商 adapter 的凭证：

- 如果启用 IBKR：IBKR Paper 用户名和密码
- 如果启用 Alpaca：Alpaca Paper API key、secret 和 `iex`/`sip` data feed

安装完成后打开：

```text
http://127.0.0.1:5173
```

默认登录账号：

```text
ati-local-user
```

密码可在 `.env` 的 `ADMIN_PASSWORD` 中查看。

### 更新版本

在首次安装时使用的同一环境中再次运行一键安装命令，并追加
`installer --update`：Windows 用户仍需在 WSL 终端中运行，macOS 和 Linux 用户在各自
终端中运行。

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/winglight/algo-trader-ib/main/scripts/install.sh)" installer --update
```

两个更新确认都默认继续，直接按回车即可。确认后，更新器会先把 MariaDB 完整逻辑备份
写入安装目录同级的 `ati-local-runtime-backups/update-<UTC时间>/`，再下载最新安装文件，
将镜像通道设为 `latest`，从 GHCR 拉取最新镜像、更新 adapter 插件并重建本地容器。

更新安装器文件时会保留 `.env`、`middle/.env`、`data`、`logs`、`strategies` 和
`middle/data`。更新期间本地应用会进入维护停机，Redis/MariaDB 保持运行以完成备份；
更新完成后服务自动恢复。任何备份失败都会终止更新，现有容器不会被替换。

无人值守更新必须同时传入 `--non-interactive` 并设置 `ATI_ALLOW_UPDATE=1`。安装器也会
检测 `unzip`；缺失时使用系统可用的 `apt-get`、`dnf`、`yum` 或 `apk` 自动安装
（非 root 用户需要 `sudo`）。

## Broker profiles

`sim` 始终启用且不需要券商账号，官方 Broker Runner 镜像默认只提供 Sim Adapter。选择 `ibkr_paper` 或 `alpaca_paper` 后，安装器会从固定来源下载、校验并安装对应插件到持久化的 `data/broker-plugins/`；不会修改官方镜像，也不会在用户机器上构建业务镜像。`ibkr_paper` 还会启动 `middle/docker-compose.yml` 中的 `ib-gateway` profile。

`ibkr_paper` 与 `alpaca_paper` 的公开源代码、能力边界和开发说明见
[Broker adapters 仓库](https://github.com/winglight/algo-trader-broker-adapters)。

安装完成后可在顶部栏查看已安装 profile。切换动作受后端 gate 和确认流程控制。只有 watchdog 容器挂载宿主机 Docker socket，其他业务容器默认不挂载。

自动化安装会自动生成基础服务密码。券商 adapter 的 secret 必须通过权限为 `0600`
或 `0400` 的文件传入；基础服务密码文件参数仍可用于显式覆盖，例如：

```bash
./setup_and_run.sh --non-interactive \
  --enabled-adapters sim,alpaca_paper \
  --initial-adapter alpaca_paper \
  --alpaca-data-feed iex \
  --redis-password-file /secure/redis \
  --mariadb-password-file /secure/mariadb \
  --admin-password-file /secure/web \
  --alpaca-api-key-id-file /secure/alpaca-key \
  --alpaca-secret-key-file /secure/alpaca-secret
```

明文 credential 命令行参数会被拒绝。可先加 `--dry-run` 验证候选配置；dry-run 不提交 env，也不启动容器。

## 管理命令

```bash
docker compose ps
curl -X POST http://127.0.0.1:8110/watchdog/actions/services/api/restart \
  -H 'Content-Type: application/json' \
  -d '{"reason":"operator_restart","source":"operator"}'
```

installer 首次整体创建可调用 Compose；运行后 backend、Broker Runner、业务服务和 frontend 的启停/重启
必须通过 watchdog action。`SERVICE_WATCHDOG_PORT` 默认在 loopback 发布为 `8110`，用于本机运维控制面，
不是业务或公网端口。`FRONTEND_PORT` 默认 `5173`；若安装时覆盖端口、network、container prefix 或 IB
Gateway 名称，installer 会把这些隔离参数持久化到 `.env`，后续 recreate 不会漂移回默认栈。

中间件在 `middle/` 下单独管理：

```bash
cd middle
docker compose ps
docker compose logs -f redis
docker compose logs -f mariadb
docker compose --profile ib logs -f ib-gateway
```

Redis 使用固定命名的持久卷保存本地安装身份、云端授权和运行租约，并启用每秒同步的 AOF。
installer 更新时会沿用当前 Redis 容器实际挂载的卷；不要执行 `docker compose down -v`
或手工删除该卷，否则系统会按新安装生成新的指纹并要求重新绑定。

## 安全边界

- 默认使用 `latest` 公开镜像；如需固定到特定版本，可在 `.env` 中设置 `ATI_IMAGE_TAG`。
- 默认发布前端 `127.0.0.1:5173` 和本机 watchdog 管理端口 `127.0.0.1:8110`；watchdog 不得绑定公网地址。
- 默认不发布 Redis、MariaDB、后端 API 服务端口。
- 默认不启用云平台、云端 Studio、Agents、AI Model Ops 或 News 服务。
- public 版本会安装 Screeners 容器，但管理员预览默认关闭。需要手动启用时，将
  `config/screeners_service.env.example` 复制为 `config/screeners_service.env`，设置
  `SCREENERS_ADMIN_PREVIEW_ENABLED=true`，并填写非空的
  `SCREENERS_GATEWAY_SHARED_SECRET`。backend 和 Screeners 都从这一份服务配置读取这两个值。
- 默认不把 Docker socket 挂载给业务容器；Docker socket 只挂载给 watchdog 容器，用于按配置重启业务容器，并在 `ib` 模式下控制 `ib-gateway`。
- 未绑定云平台用户时，本地环境只提供 24 小时试用；绑定后按云平台订阅等级控制本地服务能力。
- `.env`、`middle/.env`、`data/`、`logs/` 不应提交到公开仓库。

## 目录说明

- `docker-compose.yml`：本地应用服务。
- `middle/docker-compose.yml`：Redis、MariaDB，以及可选 IBKR Paper Gateway。
- `.env.example`：应用配置模板。
- `config/*.env.example`：各服务配置模板。
- `strategies/`：本地策略示例与自定义策略挂载目录。
- `algo_trader.sql`：本地数据库初始化 SQL。

## 免责声明

- 本项目仅用于软件开发、研究、教育和模拟交易，不构成投资建议、交易建议、要约、招揽、经纪服务或任何收益承诺。
- 交易和自动化系统存在重大风险，包括程序错误、配置错误、网络或数据中断、延迟、重复或遗漏订单、券商或第三方服务故障以及全部本金损失。Paper 交易结果不代表实盘表现。
- 您应在下单前独立核验策略、订单、账户、行情权限和风险控制，并自行判断本项目是否符合所在地法律法规、券商协议和市场规则。任何第三方名称或链接不表示其对本项目的认可或担保。
- 本项目及相关资料按“现状”和“可用状态”提供，不保证准确性、完整性、持续可用性或适用于特定目的。在适用法律允许的最大范围内，维护者和贡献者不对因使用或无法使用本项目产生的交易损失、利润损失、数据损失或其他直接、间接、附带或后果性损害负责。

## 用户协议

下载、安装、配置、访问或使用本项目，即表示您同意以下条款：

1. 您具有接受本协议的法定资格；如代表组织使用，您已获得该组织的有效授权。
2. 您仅可将本项目用于合法用途以及您有权访问的账户和数据。当前公开券商 adapters 仅限 Paper/模拟环境；不得将其用于实盘交易，也不得绕过订阅、授权、风险确认、安全控制或其他使用限制。
3. 您负责保护账户、API key、密码及本地环境安全，并对由您的配置、策略、订单和操作产生的结果承担全部责任。
4. 您须遵守适用法律法规以及 Interactive Brokers、Alpaca 和其他第三方服务各自的协议、费用、行情许可和使用政策；第三方服务的可用性和行为不受本项目控制。
5. 项目代码的复制、修改和分发同时受仓库所载开源许可证约束；云端服务、会员功能、镜像或其他产品能力可能适用其页面另行公布的产品和订阅条款。
6. 如果您不同意上述条款，请勿下载、安装、访问或使用本项目，并停止运行已部署的服务。
