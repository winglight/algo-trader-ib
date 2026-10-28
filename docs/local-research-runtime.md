# Local native research runtime / 本地原生研究环境

## Two separate execution paths / 两条不同的数据执行路径

1. **LOCAL_USER offline replay** uses `ati-local-tools`, your explicit workflow,
   execution specification, symbol/timeframe, time window and locally downloaded
   Parquet. Follow [offline replay](local-user-replay.md). This is not a certified
   Cloud Audit receipt and does not claim Studio RESEARCH_COMPUTE support by itself.
2. **Studio native research** uses `ati-local-research` and the existing native
   backtest/simulation/research kernels. Bind this environment through the URL
   printed by the connector, then select its compatible advertised capability in
   Studio. A signed frozen task uses its own checked data chunks; the installer
   does not redirect those tasks to unrelated local files. Both ends independently
   verify actual execution files and required dependency versions. Changing code,
   missing files or tampering must fail rather than forge an older version.

离线回测读取用户明确指定的本地数据，输出 `LOCAL_USER` 结果，不冒充 Cloud Audit。
Studio 研究任务继续走原生执行器、已签名任务和冻结数据块；安装器不偷换成刚下载的
Parquet。两端实际执行文件及依赖版本必须兼容，平台页面版本号不能替代执行身份。

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
old TRADING connector, connects/starts RESEARCH, and confirms the new writer.
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
