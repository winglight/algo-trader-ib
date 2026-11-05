# ATI交易系统 · 公开安装仓库 Agent 说明

本仓库是根项目 `public/` 的真实独立仓库，主要保存公开安装、IB Gateway、运行配置与 SPEC 服务安装配置。

## 工作区布局

```text
algo-trader-intelligence/
  algo-trader/
  cloud/
  ati-shared-sdk/
  algo-trader-ib/       # 当前仓库
  winglight.github.io/
```

根项目通过以下软链接访问本仓库：

```text
../algo-trader/public -> ../algo-trader-ib
```

不得在根项目中重新创建 `public` 硬拷贝，也不得把本仓库作为根项目 submodule。VPS 上保持相同的同级布局和软链接。

## 修改与发布

- 默认分支：`main`
- 普通修改直接在当前默认分支完成，不要求新建功能分支，也不要求执行“功能分支 → 合并 `main` → 生产发布”的 Cloud 工作流。
- 只有用户明确要求时才提交、推送、合并或发布。
- Docker 与安装入口：`docker/`、`scripts/`
- 运行配置：`config/`
- 本地 Agent 的 Connector、Codex/Claude CLI 和运行依赖必须全部在独立 Docker 容器内；不得安装宿主机 Agent 虚拟环境、注册 LaunchAgent/systemd 或创建裸后台代理。安装器只负责配置、镜像构建和 Compose 生命周期。
- 交易、回测、Agent 为独立可选组件；Agent 容器不得挂载宿主机主目录或 Docker socket。
- 本地策略运行：`strategy-spec-service`；旧 Python 策略模板已退役。

## ATI 产品命名

ATI 有两个独立产品，必须按语境区分，不得互相替换：

| 产品 | 中文名称 | 英文名称 |
| --- | --- | --- |
| 云端 Studio | ATI策略研究 | ATI Strategy Studio |
| 开源交易系统 | ATI交易系统 | ATI Trading System |

- 云端 Studio 的页面标题、导航品牌、账户页面、邮件及研究分享使用 Studio 名称；交易系统的本地页面、安装器、交易报告与仓库说明使用交易系统名称。
- 描述开源交易系统时可加 `开源` / `opensource` 前缀。Cloud、Local Runtime、Console、SDK 等只是组件或功能名称。
- `ATI Algotrading System` 是已停用的旧显示名称，仅在历史证据或旧文案兼容输入中保留。
- 仓库路径、域名、包名、服务标识、配置键与历史证据不随显示名称改动。
