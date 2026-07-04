# ATI Local Runtime

[English Version](README_en.md)

ATI Local Runtime 是 Algo Trading Intelligence 的本地运行包，用于在个人电脑或自有服务器上启动前端、API、账户、订单、行情、风控、策略、仿真和策略规格服务。安装时可以选择模拟 broker adapter，或连接 IBKR Paper Gateway。不包含云平台、Agents、AI Model Ops 或 News 服务。

## 安装前准备

- 已安装 Docker 和 Docker Compose 插件；如果没有安装，安装脚本会调用 `scripts/install_docker.sh`。
- 当前目录可写，用于保存 `.env`、`data/` 和 `logs/`。
- 只需要浏览器访问前端：`http://127.0.0.1:5173`。

后端服务、Redis 和 MariaDB 默认只在 Docker 网络内访问，不发布到宿主机公网端口。

## 一键安装

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/winglight/algo-trader-ib/main/scripts/install.sh)"
```

脚本会引导填写：

- Redis 密码
- MariaDB 密码
- Web 登录密码
- Broker adapter：`sim` 或 `ib`
- 如选择 `ib`，会继续填写 IBKR Paper 账号、密码和 IB Gateway VNC 密码

默认登录账号：

```text
ati-guest
```

安装完成后脚本会尝试自动打开：

```text
http://127.0.0.1:5173
```

## 管理命令

```bash
docker compose ps
docker compose logs -f frontend
docker compose logs -f backend
docker compose down
```

中间件在 `public/middle/` 下单独管理：

```bash
cd middle
docker compose ps
docker compose logs -f redis
docker compose logs -f mariadb
```

## 安全边界

- 默认不暴露后端 API 端口。
- 默认不暴露 Redis/MariaDB 端口。
- 后端 docs/redoc/openapi 默认关闭。
- `sim` 模式使用 `src.broker_adapters.sim:create_adapter`；`ib` 模式使用 `src.broker_adapters.ibkr_paper:create_adapter` 并只连接 Paper Gateway。
- `.env`、`middle/.env`、`data/`、`logs/` 不应提交到公开仓库。
- 本地公开包不包含云平台服务、云端镜像或私有平台代码。

## 目录说明

- `docker-compose.yml`：本地应用服务。
- `middle/docker-compose.yml`：Redis、MariaDB，以及可选的 IBKR Paper Gateway。
- `.env.example`：应用配置模板。
- `config/*.env.example`：各服务配置模板。
- `strategies/`：本地策略示例与自定义策略挂载目录。
- `algo_trader.sql`：本地数据库初始化 SQL。

## 更新

```bash
docker compose pull
docker compose up -d
```

如果数据库结构变更，需要按发布说明备份并迁移 `middle/data/mariadb`。
