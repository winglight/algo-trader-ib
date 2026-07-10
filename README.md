# ATI Local Runtime

[English Version](README_en.md)

ATI Local Runtime 是 Algo Trading Intelligence 的本地运行版。它把交易工作台、API、账户、订单、行情、风控、策略、仿真和策略规格服务打包为一套 Docker Compose 环境，适合在个人电脑或自有服务器上运行。

官方网站：[ati.broyustudio.com](https://ati.broyustudio.com)  
会员与产品页入口：[ati.broyustudio.com](https://ati.broyustudio.com)  
云端 Strategy Studio：[ati-studio.broyustudio.com](https://ati-studio.broyustudio.com)  
本地交易系统 Demo：[ati-trading.broyustudio.com](https://ati-trading.broyustudio.com)

本地版用于运行和验证自己的交易环境；云端 Studio 用于托管式 workflow 设计、云端试用和后续订阅能力。公开安装包不包含云平台服务、云端镜像、Agents、AI Model Ops、News 或私有平台代码。

首次安装后，本地环境在未绑定云平台用户前可试用 24 小时。绑定云平台用户后，本地服务会按云平台订阅等级下发的能力启用或禁用。

![ATI Local Runtime](images/screenshot.png)

## 功能概览

- 本地交易工作台：浏览器访问 `http://127.0.0.1:5173`。
- Broker adapter 可选：默认 `sim` 模拟交易，也可选择 `ib` 连接 IBKR Paper Gateway。
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

安装脚本会引导填写：

- Redis 密码
- MariaDB 密码
- Web 登录密码
- Broker adapter：`sim` 或 `ib`
- 如果选择 `ib`：IBKR Paper 用户名、密码和 IB Gateway VNC 密码

安装完成后打开：

```text
http://127.0.0.1:5173
```

默认登录账号：

```text
ati-guest
```

密码为安装时填写的 Web 登录密码。

## Broker 模式

`sim` 是默认模式，不需要券商账号。主应用会启动 `service-watchdog`，并且只有 watchdog 容器会挂载宿主机 Docker socket，用于受控重启业务容器；其他业务容器默认不挂载 Docker socket。

`ib` 模式会额外启动 `middle/docker-compose.yml` 中的 `ib-gateway` profile，并允许 watchdog 通过页面或接口控制 `ib-gateway` start/stop/restart。

## 管理命令

```bash
docker compose ps
docker compose logs -f frontend
docker compose logs -f backend
docker compose restart backend
docker compose down
```

中间件在 `middle/` 下单独管理：

```bash
cd middle
docker compose ps
docker compose logs -f redis
docker compose logs -f mariadb
docker compose --profile ib logs -f ib-gateway
```

## 安全边界

- 默认使用 `latest` 公开镜像；如需固定到特定版本，可在 `.env` 中设置 `ATI_IMAGE_TAG`。
- 默认只发布前端端口 `127.0.0.1:5173`。
- 默认不发布 Redis、MariaDB、后端 API 服务端口。
- 默认不启用云平台、云端 Studio、Agents、AI Model Ops 或 News 服务。
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

## 更新

```bash
docker compose pull
docker compose up -d
```

如果使用 IB 模式：

```bash
docker compose --profile ib pull
docker compose --profile ib up -d
```

如果数据库结构变更，请先备份 `middle/data/mariadb`，再按发布说明迁移。
