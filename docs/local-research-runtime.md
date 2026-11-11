# Local native research runtime / 本地原生研究环境

## Two separate execution paths / 两条不同的数据执行路径

1. **LOCAL_USER offline replay** uses `ati-local-tools`, your explicit workflow,
   execution specification, symbol/timeframe, time window and locally downloaded
   Parquet. Follow [offline replay](local-user-replay.md). This is not a certified
   Cloud Audit receipt and does not claim Studio RESEARCH_COMPUTE support by itself.
2. **Studio native research** uses `ati-local-research` and the existing native
   backtest/simulation/research kernels. Bind this environment through the URL
   printed by the connector, then select its compatible advertised capability in
   Studio. Studio backtests and simulations read your existing Parquet directory, shared with LTB.
   Both ends independently
   verify actual execution files and required dependency versions. Changing code,
   missing files or tampering must fail rather than forge an older version.

离线回测读取用户明确指定的本地数据，输出 `LOCAL_USER` 结果，不冒充 Cloud Audit。
Studio 本地研究任务通过原生执行器读取用户在 LTB 所用目录准备的 Parquet。两端实际执行文件及依赖版本必须兼容。

## Installation and upgrade / 安装与升级

```sh
# Run these yourself on the intended machine; installation starts its selected services.
bash setup_and_run.sh --components research
# Or choose trading / all (default); an update keeps previously installed components.
bash scripts/install_research.sh status
bash scripts/install_research.sh upgrade
```

For a versioned release, `ATI_UPDATE_IMAGE_TAG` selects the same published version
for the tools and native research image. `ATI_RESEARCH_IMAGE` is an explicit image
override. The installer pulls the distributable image; it does not publish private
Cloud sources. It does not collect market-data keys or automatically download bars.
Use [the download guide](local-data-download.md) / [中文下载说明](local-data-download.zh-CN.md)
to run the three provider scripts with your own credentials and chosen lists.
A headless research install never queries a trading license or creates a
`trading-enabled` marker. An existing all-mode install retains its trading identity.

先安装离线工具并记录已安装组件，再连接 Studio。若绑定失败，安装命令返回非零，
不会显示全部成功；已安装的工具和历史数据仍保留，可独立使用。不要把 `.research.env`
文件存在当作已连接或具有研究能力的证明。实际就绪以环境连接及能力/兼容校验为准。

## One identity, one connector writer / 同一身份只允许一个 connector

The installer serializes mutations with a portable OS file lock. It checks the
fixed compose service labels, exact identity bind mount and container state. A
foreign/ambiguous writer is not stopped; the operation fails for explicit diagnosis.
Other installation identities are left alone. The handoff stops only the verified
old TRADING connector or the existing RESEARCH coordinator during pool replacement,
connects/starts RESEARCH, and confirms the single new writer.
A failed operation reads back state before rollback. It restores only the exact old
container when safe; unknown stop/start outcomes require status inspection rather
than blind retries. Existing research upgrades retain their own compose service.

这个交接只涉及环境 connector，不重启交易服务、不更换账户、不下单、不清除风控，
也不删除历史账本/数据。若旧 writer 状态、标签、身份挂载不确定，安装会停止并提示
检查，不会自动停止未知容器或再启动第二个 writer。配置文件按数据解析，不作为 shell 执行。

## Verification limits / 验收边界

Source tests cover mocked install/upgrade/failure paths and isolated native execution.
They do not replace an actual clean-machine Docker build/install, real provider data
licensing checks or bilingual screen recordings. Those items must be reported separately.
本说明不是已完成镜像发布、生产部署、真实下载或中英文录屏的声明。


## Container pool and total budgets / 多容器执行池与总资源预算

Studio research now uses **N serial executor containers plus one lightweight
coordinator**. N is the smaller of the current membership concurrency grant,
protocol limit (32), and the number fitting the Docker resource budget. A single
executor runs only one task. The coordinator alone owns the environment identity;
executor containers never mount credentials, the host home, or the Docker socket,
and have no network. Market data is read-only. Tasks use independent container
filesystems and a root-only shared queue. No host background agent is installed.

安装器连接后只读查询会员容量，不领取真实研究任务、不发心跳伪造容量。根据 Docker
实际分配的 CPU/内存，计算 N 并保存到 `.research.env`，通过 Compose 的
`--scale research-executor=N` 启动。先确认全部执行副本的标签、所属安装、共享卷、
只读行情挂载及运行状态，再启动唯一协调器。配置文件或单个容器存在不代表安装成功。

Defaults per executor are **0.5 CPU and 1536 MiB**, with a **0.25 CPU / 256 MiB**
coordinator. Fresh installs use automatic total budgets: Docker CPUs, and Docker
memory minus the larger of 512 MiB or 10% headroom. Historical installations without
saved budgets retain the old **2 CPU / 4 GiB total**, which fits two executors.
`ATI_RESEARCH_CPUS` and `ATI_RESEARCH_MEMORY` always mean **total budgets including
the coordinator**, never a per-replica allocation silently multiplied by N.

The installer reports the membership limit, CPU capacity, memory capacity, and
actual executor count. When capacity is smaller than membership, increase an
explicit total budget or Docker's allocated resources. Membership changes are
always enforced by Cloud on new task claims across all of the user's devices;
run `upgrade` again to resize the local container pool after a plan change.

```sh
# Explicit total budget; installation starts the selected research containers.
ATI_RESEARCH_CPUS=4 ATI_RESEARCH_MEMORY=8g bash scripts/install_research.sh upgrade
# Explicit CPU oversubscription up to 2x Docker CPUs. Memory remains a hard bound.
ATI_RESEARCH_CPUS=auto ATI_RESEARCH_CPU_OVERCOMMIT=2 bash scripts/install_research.sh upgrade
# Stop both the coordinator and all verified executors; no trading service is stopped.
bash scripts/install_research.sh stop
```

Advanced configuration: `ATI_RESEARCH_EXECUTOR_CPUS`,
`ATI_RESEARCH_EXECUTOR_MEMORY`, `ATI_RESEARCH_COORDINATOR_CPUS`,
`ATI_RESEARCH_COORDINATOR_MEMORY`, `ATI_RESEARCH_EXECUTOR_TMPFS_MB` (default 512),
and optional `ATI_RESEARCH_MAX_PARALLEL`. CPU overcommit defaults to 1 and must be
explicitly increased (maximum 8); it can reduce throughput per task and increase
latency. It does not override an explicit smaller total CPU budget. Memory cannot
be oversold, and all container memory limits stay within the configured total.
A task exceeding its individual container limit can fail; lower its concurrency
or raise its per-container budget rather than treating an OOM as a valid result.

升级会先停止协调器和旧执行池，再重新创建/缩放执行容器。未完成的计算可能中断，
历史记录与已完成结果保留，按原有租约/重试规则恢复。未知状态、错误身份、意外凭据
挂载或副本不足都返回失败，不盲目启动第二个身份 writer 或自动干预未知容器。

Cloud must expose the read-only `GET /cloud/local-execution/capacity` protocol,
and the research image must include the pool coordinator and executor. Upgrade
all Cloud claim instances before using the new image. Older Cloud/images fail
closed with an upgrade message rather than guessing membership capacity. Source
commits do not publish the required runtime image or deploy Cloud.
