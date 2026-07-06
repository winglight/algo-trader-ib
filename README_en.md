# ATI Local Runtime

[中文说明](README.md)

ATI Local Runtime is the local runtime package for Algo Trading Intelligence. It runs the trading workspace, API, account, orders, market data, risk, strategy, simulation, and strategy spec services as a Docker Compose environment on your own machine or server.

Official website: [ati.broyustudio.com](https://ati.broyustudio.com)  
Cloud console: [ati-cloud.broyustudio.com](https://ati-cloud.broyustudio.com)  
Cloud Strategy Studio: [ati-studio.broyustudio.com](https://ati-studio.broyustudio.com)

Use the local runtime for your own trading environment and local validation. Use Cloud Strategy Studio for hosted workflow design, trial access, and future subscription features. This public package does not include cloud platform services, cloud images, Agents, AI Model Ops, News, or private platform code.

After the first installation, the local environment can run for 24 hours before it is bound to a cloud account. After binding, local services are enabled or disabled according to the capabilities attached to the cloud subscription tier.

![ATI Local Runtime](images/screenshot_en.png)

## Features

- Local trading workspace at `http://127.0.0.1:5173`.
- Broker adapter selection: default `sim` paper-like simulator, or `ib` for IBKR Paper Gateway.
- Core runtime services: API, account, orders, market data, risk, strategy, simulation, and strategy spec.
- Local strategy mount: `strategies/` is mounted into the containers for examples and custom strategies.
- Local persistence: `.env`, `middle/.env`, `data/`, and `logs/` stay on your machine.
- Backend docs/redoc/openapi routes are disabled by default, and backend, Redis, and MariaDB ports are not published to the host by default.

![PnL Calendar](images/pnl-calendar.png)

## One-Line Install

Copy this line into your terminal:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/winglight/algo-trader-ib/main/scripts/install.sh)"
```

The installer asks for:

- Redis password
- MariaDB password
- Web login password
- Broker adapter: `sim` or `ib`
- If `ib` is selected: IBKR Paper username, password, and IB Gateway VNC password

After installation, open:

```text
http://127.0.0.1:5173
```

Default username:

```text
ati-guest
```

Use the web login password you entered during installation.

## Broker Modes

`sim` is the default mode. It does not require a broker account and does not mount the host Docker socket.

`ib` starts the `ib-gateway` profile from `middle/docker-compose.yml` and enables the main `service-watchdog` profile. That profile mounts the host Docker socket only into the watchdog container so the app can start, stop, or restart `ib-gateway`; application containers do not mount the Docker socket by default.

## Operations

```bash
docker compose ps
docker compose logs -f frontend
docker compose logs -f backend
docker compose restart backend
docker compose down
```

Middleware is managed under `middle/`:

```bash
cd middle
docker compose ps
docker compose logs -f redis
docker compose logs -f mariadb
docker compose --profile ib logs -f ib-gateway
```

## Security Boundary

- Public images default to the `latest` tag; set `ATI_IMAGE_TAG` in `.env` when you intentionally pin a specific release.
- Only the frontend port `127.0.0.1:5173` is published by default.
- Redis, MariaDB, and backend API service ports are not published by default.
- Cloud platform, Cloud Studio, Agents, AI Model Ops, and News services are not started by default.
- Application containers do not mount the Docker socket by default. If `ib` mode is selected, the watchdog container mounts the Docker socket to control `ib-gateway`.
- Before cloud account binding, the local environment has a 24-hour trial window. After binding, local runtime capabilities follow the cloud subscription tier.
- Do not commit `.env`, `middle/.env`, `data/`, or `logs/`.

## Files

- `docker-compose.yml`: local application services.
- `middle/docker-compose.yml`: Redis, MariaDB, and optional IBKR Paper Gateway.
- `.env.example`: application configuration template.
- `config/*.env.example`: per-service configuration templates.
- `strategies/`: local example and custom strategy mount directory.
- `algo_trader.sql`: local database initialization SQL.

## Update

```bash
docker compose pull
docker compose up -d
```

For IB mode:

```bash
docker compose --profile ib pull
docker compose --profile ib up -d
```

When a release changes the database schema, back up `middle/data/mariadb` before following the release migration notes.
