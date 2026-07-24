# Algo Trader IB / Public Agent 说明

本仓库是根项目 `public/` 的真实独立仓库，主要保存公开安装、IB Gateway、运行配置与公共策略资源。

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
- Docker 与安装入口：`docker/`、`scripts/`
- 运行配置：`config/`
- 公共策略：`strategies/`
