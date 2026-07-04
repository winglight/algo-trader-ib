# ATI Local Runtime

[中文说明](README.md)

ATI Local Runtime is the local simulated runtime package for Algo Trading Intelligence. It starts the frontend, API, account, orders, market data, risk, strategy, simulation, and strategy spec services. It uses the simulated broker adapter by default and does not include the cloud platform, Agents, AI Model Ops, or News services.

## Requirements

- Docker with the Docker Compose plugin.
- A writable working directory for `.env`, `data/`, and `logs/`.
- Browser access to `http://127.0.0.1:5173`.

Backend services, Redis, and MariaDB are reachable only inside the Docker network by default.

## Install

```bash
./setup_and_run.sh
```

The installer asks for:

- Redis password
- MariaDB password
- Web login password

Default login user:

```text
ati-guest
```

Open the app after installation:

```text
http://127.0.0.1:5173
```

## Operations

```bash
docker compose ps
docker compose logs -f frontend
docker compose logs -f backend
docker compose down
```

Middleware is managed under `public/middle/`:

```bash
cd middle
docker compose ps
docker compose logs -f redis
docker compose logs -f mariadb
```

## Security Boundary

- Backend API ports are not published by default.
- Redis and MariaDB ports are not published by default.
- The default broker adapter is `src.broker_adapters.sim:create_adapter`.
- Do not commit `.env`, `middle/.env`, `data/`, or `logs/`.
- This local package does not include cloud platform services, cloud images, or private platform code.

## Files

- `docker-compose.yml`: local application services.
- `middle/docker-compose.yml`: Redis and MariaDB.
- `.env.example`: application configuration template.
- `config/*.env.example`: per-service configuration templates.
- `strategies/`: local example and custom strategy mount directory.
- `algo_trader.sql`: local database initialization SQL.

## Update

```bash
docker compose pull
docker compose up -d
```

When a release changes the database schema, back up and migrate `middle/data/mariadb` according to the release notes.
