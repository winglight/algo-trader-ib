# ATI Local Runtime

[中文说明](README_cn.md)

ATI Local Runtime is the local runtime package for Algo Trading Intelligence. It runs the trading console, API, account, orders, market data, risk, strategy, simulation, and strategy spec services as a Docker Compose environment on your own machine or server.

Official website: [ati.broyustudio.com](https://ati.broyustudio.com)  
Membership and product page: [ati.broyustudio.com](https://ati.broyustudio.com)  
Cloud Strategy Studio: [ati-studio.broyustudio.com](https://ati-studio.broyustudio.com)  
Local trading system demo: [ati-trading.broyustudio.com](https://ati-trading.broyustudio.com)

Open-source Broker adapters: [winglight/algo-trader-broker-adapters](https://github.com/winglight/algo-trader-broker-adapters)

Use the local runtime for your own trading environment and local validation. Use Cloud Strategy Studio for hosted workflow design, trial access, and future subscription features. 

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

The installer automatically generates the Redis, MariaDB, web login, and IB
Gateway VNC passwords and writes them to `.env` or `middle/.env` with mode
`0600`. Existing non-empty passwords are preserved when the installer is rerun
or an installation is updated.

Interactive installation asks only for the enabled and initial adapters and the
credentials required by the selected broker adapters:

- If IBKR is enabled: IBKR Paper username and password
- If Alpaca is enabled: Alpaca Paper API key, secret, and `iex`/`sip` data feed

After installation, open:

```text
http://127.0.0.1:5173
```

Default username:

```text
ati-local-user
```

Read `ADMIN_PASSWORD` from `.env` for the web login password.

## Broker Profiles

`sim` is always enabled and requires no broker account; the official Broker Runner image provides only the Sim Adapter by default. After `ibkr_paper` or `alpaca_paper` is selected, the installer downloads, verifies, and installs that plugin into the persistent `data/broker-plugins/` directory. It neither modifies the official image nor builds a business-service image on the user's machine. `ibkr_paper` also starts the `ib-gateway` profile from `middle/docker-compose.yml`.

The public source code, capability boundaries, and development documentation for
`ibkr_paper` and `alpaca_paper` are available in the
[Broker adapters repository](https://github.com/winglight/algo-trader-broker-adapters).

Installed profiles are shown in the top bar. Adapter changes use the backend gate and confirmation flow. Only the watchdog container mounts the host Docker socket; application containers do not mount it.

Non-interactive installation generates service passwords automatically. Broker
adapter secrets must be supplied through files with mode `0600` or `0400`.
Service password file options remain available as explicit overrides:

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

### Updating an existing installation

Append `installer --update` to the one-line installer command to update an existing
installation. Both update confirmations default to yes, so pressing Enter continues.
After confirmation,
the installer writes a complete logical MariaDB backup to the sibling directory
`ati-local-runtime-backups/update-<UTC timestamp>/`, pins the update channel to
`latest`, pulls current images from GHCR, and recreates the local containers. A
backup failure aborts the update before any running container is replaced. The
installer preserves the `data`, `logs`, `strategies`, and `middle/data` runtime
directories while replacing installer files. After confirmation, the local
application enters maintenance downtime while Redis and MariaDB remain online
for the backup; services are restored automatically when the update completes.

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/winglight/algo-trader-ib/main/scripts/install.sh)" installer --update
```

Unattended updates additionally require `--non-interactive` and
`ATI_ALLOW_UPDATE=1`.

The installer checks for `unzip` and, when it is missing, installs it through
the available `apt-get`, `dnf`, `yum`, or `apk` package manager. Non-root users
need `sudo` for this step.

## Disclaimer

- This project is provided solely for software development, research, education,
  and simulated trading. It is not investment or trading advice, an offer or
  solicitation, a brokerage service, or a promise of returns.
- Trading and automated systems involve substantial risk, including software or
  configuration errors, network or data outages, latency, duplicate or missed
  orders, broker or third-party failures, and loss of all capital. Paper-trading
  results do not represent live-trading performance.
- You must independently verify strategies, orders, accounts, market-data
  permissions, and risk controls before submitting any order, and determine
  whether your use complies with applicable law, broker agreements, and market
  rules. Third-party names and links do not imply endorsement or warranty.
- The project and its materials are provided "as is" and "as available", without
  warranties of accuracy, completeness, availability, or fitness for a
  particular purpose. To the fullest extent permitted by law, maintainers and
  contributors are not liable for trading losses, lost profits, lost data, or
  any direct, indirect, incidental, or consequential damages arising from use
  of, or inability to use, the project.

## User Agreement

By downloading, installing, configuring, accessing, or using this project, you
agree that:

1. You have legal capacity to accept these terms and, if acting for an
   organization, authority to bind that organization.
2. You will use the project only for lawful purposes and with accounts and data
   you are authorized to access. The currently published broker adapters are
   Paper/simulation only; you must not use them for live trading or bypass
   subscriptions, licensing, risk confirmations, security controls, or other
   usage restrictions.
3. You are responsible for securing accounts, API keys, passwords, and the local
   environment, and for all results caused by your configurations, strategies,
   orders, and operations.
4. You will comply with applicable laws and with the separate agreements, fees,
   market-data licenses, and policies of Interactive Brokers, Alpaca, and other
   third parties. This project does not control their availability or behavior.
5. Copying, modification, and distribution of source code remain subject to the
   repository's open-source license. Cloud services, membership features,
   images, or other product capabilities may be subject to separate product and
   subscription terms published with those services.
6. If you do not agree to these terms, do not download, install, access, or use
   the project, and stop any deployed services.
