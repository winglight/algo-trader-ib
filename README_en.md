# ATI Local Runtime

[中文说明](README.md)

ATI Local Runtime is the local runtime package for Algo Trading Intelligence. It runs the trading console, API, account, orders, market data, risk, strategy, simulation, and strategy spec services as a Docker Compose environment on your own machine or server.

Official website: [ati.broyustudio.com](https://ati.broyustudio.com)  
Membership and product page: [ati.broyustudio.com](https://ati.broyustudio.com)  
Cloud Strategy Studio: [ati-studio.broyustudio.com](https://ati-studio.broyustudio.com)  
Local trading system demo: [ati-trading.broyustudio.com](https://ati-trading.broyustudio.com)

Use the local runtime for your own trading environment and local validation. Use Cloud Strategy Studio for hosted workflow design, trial access, and future subscription features. This public package does not include cloud platform services, cloud images, Agents, AI Model Ops, News, or private platform code.

After the first installation, the local environment can run for 24 hours before it is bound to a cloud account. After binding, local services are enabled or disabled according to the capabilities attached to the cloud subscription tier.

![ATI Local Runtime](images/screenshot_en.png)

## Features

- Local trading console at `http://127.0.0.1:5173`.
- Broker profiles: `sim`, `ibkr_paper`, and `alpaca_paper`; `sim` is always installed and multiple Paper profiles may be enabled together.
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
- Whether to enable IBKR Paper and Alpaca Paper
- Initial adapter profile
- If IBKR is enabled: IBKR Paper username, password, and IB Gateway VNC password
- If Alpaca is enabled: Alpaca Paper API key, secret, and `iex`/`sip` data feed

After installation, open:

```text
http://127.0.0.1:5173
```

Default username:

```text
ati-guest
```

Use the web login password you entered during installation.

## Broker Profiles

`sim` is always enabled and does not require a broker account. `ibkr_paper` additionally starts the `ib-gateway` profile from `middle/docker-compose.yml`. `alpaca_paper` adds no container; only when selected, the installer builds a local derived Broker Runner image from a pinned commit and checksum.

Installed profiles are shown in the top bar. Adapter changes use the backend gate and confirmation flow. Only the watchdog container mounts the host Docker socket; application containers do not mount it.

Non-interactive installation accepts secrets only through files with mode `0600` or `0400`:

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

Plaintext credential arguments are rejected. Add `--dry-run` to validate candidate configuration without committing env files or starting containers.

## Operations

```bash
docker compose ps
curl -X POST http://127.0.0.1:8110/watchdog/actions/services/api/restart \
  -H 'Content-Type: application/json' \
  -d '{"reason":"operator_restart","source":"operator"}'
```

The installer may use Compose for initial stack creation. After startup,
backend, Broker Runner, business-service, and frontend lifecycle actions must go
through watchdog HTTP actions. `SERVICE_WATCHDOG_PORT` defaults to loopback port
`8110`; it is a local operations control plane, not a public business endpoint.
`FRONTEND_PORT` defaults to `5173`. Installer overrides for ports, network,
container prefix, and IB Gateway name are persisted to `.env` so later recreates
remain isolated.

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
- The frontend `127.0.0.1:5173` and local watchdog management port `127.0.0.1:8110` are published by default; watchdog must never bind to a public interface.
- Redis, MariaDB, and backend API service ports are not published by default.
- Cloud platform, Cloud Studio, Agents, AI Model Ops, and News services are not started by default.
- Application containers do not mount the Docker socket by default. The Docker socket is mounted only into the watchdog container, which restarts configured application containers and controls `ib-gateway` when `ib` mode is selected.
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
