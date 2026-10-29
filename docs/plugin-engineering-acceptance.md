# 插件工程验收

本轮以已合并的 Rust 插件宿主提交 `3a390d6ce1f1266ab6435fa9e8e4cf17f17f52ac` 为基线。保留该实现的 Rust manifest、依赖与作用域管理、C ABI 和 Python 业务适配，不引入第二套运行时。原先基于旧提交的草稿不在本次交付中。

## 交付范围

当前产品通过 `config/agent-profiles/knowledge.json` 装配模型、上下文、预算、会话、授权网关、工具、loop 和知识库应用。仍只启用知识库产品需要的组合，没有新增多 Agent、沙箱、任意目录插件发现或运行中透明热替换。插件必须来自可信 catalog；插件注册不构成恶意代码隔离。

本轮工程变更：

- 事件分发持有租约直到监听器完成，避免关闭释放仍在使用的父级或本地资源。
- 租约绑定实际线程，复制 ContextVar 到新线程仍需重新准入；排空后不能借用另一线程的活动租约启动新工作。
- 注册、发布、移除元数据使用作用域锁，清理回调仍在锁外执行；失败清理保留未释放资源，继续支持重试关闭。清理所需的 `ctx.require()` 保持可用。
- Rust 在作用域创建时构建服务归属索引和活跃插件集合，替代每次解析时扫描全部插件和服务；保留配置顺序、显式覆盖和激活顺序。
- CI 加入 Clippy 警告门禁，Python 合同使用本轮独立 release 构建的库副本。构建失败或制品缺失时明确阻断合同，不能加载旧库得到绿色结果。每轮 target 隔离，避免并发构建替换待验收制品。

接口、模型可见 Schema、知识库规划策略、工具业务、权限检查、证据格式、请求账本与预算策略均未在本轮修改。通用工具和观察者仍同步执行，deadline 与取消不承诺抢占同步函数或撤回已经发送的请求。

## 行为验收

固定依赖：Python 3.12.14、Rust 1.99.0，使用仓库 `uv.lock` 与 `Cargo.lock`。基线统一门禁通过 266 个 Python 组件合同；本轮增加 28 个：

| 验证模块 | 新增数 | 主要边界 |
| --- | ---: | --- |
| `plugin_host_contracts.py` | 6 | 事件在途关闭、父资源保活、嵌套排空、跨线程上下文复制、异常归还租约、挂载期间事件拒绝 |
| `quality_contracts.py` | 3 | 构建失败不加载旧库、缺失制品失败、每轮目标和制品副本隔离 |
| `agent_context_contracts.py` | 6 | 工具组完整性、角色隔离、压缩原文与增量 checkpoint |
| `agent_store_contracts.py` | 4 | 真实 SQLite 并发写者、跨会话隔离、新实例幂等、失效租约拒绝写入 |
| `arithmetic_contracts.py` | 2 | AST 注入拒绝、资源上限、十进制语义 |
| `knowledge_lifecycle_contracts.py` | 5 | 真实 profile/native loop 的关闭、重启、撤权、预算失败、初始化资源归属 |
| `harness_performance_contracts.py` | 2 | 测量失败保留在分母、错误结果不计成功、无自动重试 |

Rust 新增 5 个测试，覆盖最大 256 插件目录、未激活覆盖不回退、清理依赖可查而新租约拒绝、跨作用域错误租约不改变状态，以及深层继承与排空。原有测试未删除。

统一入口 `uv run --no-sync python scripts/check_project.py` 执行架构、Rust fmt、Clippy、独立 release 构建、Rust 测试、Ruff、Python 合同和评分器控制。成功标准为 **40 个 Rust 测试、294 个 Python 组件合同、24 个冻结评分器控制全部通过，零跳过**。每次运行在新的 `.local/checks/<编号>/` 保留 summary、日志、组件结果及确切库副本；CI 上传同一产物。

测试使用真实本地组件与合成模型/来源，没有新增模型、Embedding 或 judge 请求。24 条控制 trace 重新评分；这不代表生产 RAG 质量。Store 的“重启”测试是新建实例，不冒充 OS 崩溃恢复；平台沙箱和上游项目测试不在本轮范围。

## 性能对照

使用同一 `kb_platform.rag_testing.harness_performance` 脚本，在基线和修改后工作树分别加载对应 release 动态库。每版 5 个独立进程、22,500 个计时样本和 900 次预热。先运行整批基线/候选，再交错运行 5 轮匹配负载，顺序交替，两批结果均保留；没有挑选成功请求或修改阈值。

交错复测的进程分位数中位数如下，单位为微秒：

| 负载 | 插件数 | p50 基线 → 本轮 | p95 基线 → 本轮 |
| --- | ---: | ---: | ---: |
| 最后注册服务的热调用 | 1 | 110.074 → 103.873 | 186.633 → 176.954 |
| 同上 | 32 | 117.612 → 109.989 | 203.577 → 186.832 |
| 同上 | 128 | 113.618 → 107.913 | 172.963 → 172.144 |
| 同上 | 256 | 112.615 → 104.499 | 182.377 → 151.834 |
| 子 scope 创建、继承调用、关闭 | 1 | 367.421 → 351.570 | 475.484 → 486.943 |
| 同上 | 32 | 394.000 → 366.543 | 558.154 → 558.389 |
| 同上 | 128 | 395.044 → 378.049 | 567.090 → 521.301 |
| 同上 | 256 | 419.102 → 412.598 | 621.670 → 679.503 |
| single-tool 完整轮次 | 4 | 1880.280 → 1934.710 | 2357.299 → 2542.279 |

热服务调用的收益在两批中都可观察；完整轮次没有加速，不能据此宣称整体性能提升。完整轮次的进程 p50 范围为基线 1847.783–2001.182、本轮 1914.468–1966.202 微秒，交错轮次 p50 变化中位数为 +3.06%。scope 128 的修改后一次进程 p95 为 2504.360 微秒，原始结果保留该异常值。新增租约保护属于本轮候选的一部分，不能把全部变化单独归因于 Rust 索引。

这些测量包含 Python/JSON/Rust ABI，排除模型、网络、检索和持久化。环境未随机化或完全隔离宿主调度；累计进程峰值 RSS 约 21 MB，不是单个插件占用。没有设置性能准入阈值，也不声称达到 Codex 或 DSH 的吞吐。

复现命令（输出目录必须不存在）：

```bash
cargo build --workspace --release --locked --offline
uv run --no-sync python -m kb_platform.rag_testing.harness_performance \
  --library target/release/libagent_kernel.so --build-profile release \
  --processes 5 --output .local/harness-performance/new-run
```

Windows/macOS 替换动态库文件名；Windows 上无法取得 `resource.ru_maxrss` 时明确记录 unavailable。每次结果包含计划、逐请求耗时、失败、机器信息、源码与库哈希。此次本地聚合记录为 `.local/harness-performance/interleaved-release-02-summary.json`，首次候选记录为 `.local/harness-performance/indexed-release-01/`；私有运行目录不随仓库发布，其他环境需重新执行。

## 工程参考与保留边界

生命周期和效果释放参考 [DSH 固定版本架构](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/docs/architecture.md)，工具与上下文边界参考 [Pi Agent Loop](https://github.com/earendil-works/pi/blob/e7a0df27c09d11bf4161d3605246cbf57a129cb2/packages/agent/src/agent-loop.ts#L172)，并发行为验收参考 [Codex 慢连接隔离测试](https://github.com/openai/codex/blob/6174d05a6a17255dda97be686072eee00c4a1bc9/codex-rs/app-server/src/transport_tests.rs#L469)。本项目没有复制其运行时，也不兼容 DSH TypeScript 插件 ABI。

后续新增能力应通过现有可信 catalog 和声明式依赖接入，继续遵守宿主权限、物理请求和证据合同；本轮不为尚无产品需求的能力添加空插件。
