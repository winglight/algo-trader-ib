# 使用 GHCR 镜像一键运行（public 目录）

- 进入 `public` 目录并执行：
  - `bash ./setup_and_run.sh`

- 脚本能力：
  - 自动复制 `middle/.env.example` 与 `public/.env.example` 为 `.env`
  - 交互输入并写入：`TWS_USERID`、`TWS_PASSWORD`、`VNC_SERVER_PASSWORD`、`REDIS_PASSWORD`、`MARIADB_PASSWORD`
  - 启动基础设施：`ib-gateway`、`redis`、`mariadb`
  - 在 MariaDB 就绪后创建数据库与用户：`algo_trader`，并导入 `public/mariadb_init.sql`
  - 更新 `public/.env` 中的 `REDIS_URL` 与 `MARIADB_URL`
  - 启动所有服务容器（前后端）：`docker compose -f public/docker-compose.yml up -d`

- 注意事项：
  - 如需使用你自己的 GHCR 命名空间，请将 `public/docker-compose.yml` 中镜像前缀替换为 `ghcr.io/<你的GitHub用户名>`
  - 若网络 `stack` 不存在，脚本会由 Compose 自动创建；默认子网为 `172.21.0.0/16`