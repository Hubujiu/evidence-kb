# Rust 插件改造验收

本轮从 `cbd8623f4907ae36e0e1b65bb990fa3e33875620` 实施 [执行计划](evidence-kb-rust-plugin-execution-plan.md)。V1 内置插件和 E0 进程插件已实现，当前分支默认入口使用同一个 Rust 生命周期运行时。代码通过分层 PR 交付；尚未合并到 `main`。E1 的不可信或可移植插件需求没有成立，按计划保留为条件扩展。

## 交付与合并顺序

| PR | 交付 | 实施提交 | 前置 |
| --- | --- | --- | --- |
| [#16](https://github.com/Hubujiu/evidence-kb/pull/16) | R0 来源、合同和基线 | `de71df7` | #15 |
| [#17](https://github.com/Hubujiu/evidence-kb/pull/17) | R1–R6 Rust 运行时与 Python 桥 | `ea57cd2` | #16 |
| [#18](https://github.com/Hubujiu/evidence-kb/pull/18) | E0 可安装进程插件 | `4432504` | #17 |
| [#19](https://github.com/Hubujiu/evidence-kb/pull/19) | B1/B7 模型与通用 Agent | `c5920ca` | #18 |
| [#20](https://github.com/Hubujiu/evidence-kb/pull/20) | B0/B2–B6/B8/C0–C2 知识库根组合 | `5d18294` | #19 |

每个 PR 的 base 指向前置分支。通用 Agent 与知识库 workflow 分开交付；没有强制推送、绕过审查或合并默认分支。PR #14 的模型模块已在当前基线核对后移入，旧分支仍保持原状。合并需按仓库开发约定取得 reviewer 批准并通过 required 检查。

## 工作包与证据

| 工作包 | 实际交付 | 验证位置 |
| --- | --- | --- |
| R0 | 锁定 DSH/Cordis 来源、20 个原始文件及九个实际运行轨迹；冻结旧源码、release 库、请求与性能基线 | `config/plugin-runtime/cordis-traces-v1.json`、`cordis-reference-lock.json`、[来源与基线](rust-plugin-provenance.md)，九项参考合同 |
| R1 | 独立 `rust/plugin-runtime`；实例、代次、注册、租约、资源和严格清理；Tokio 停止并等待示例 | Rust coordinator 合同、`rust/plugin-runtime/examples/owned_task.rs` |
| R2 | `plugin-runtime/1` 动作协议；Python 在锁外执行；既有 SQLite SessionStore 由新内核创建、读写、关闭 | `runtime_bridge_contracts.py`、`plugin_host_contracts.py` |
| R3 | 迟到初始化、取消、deadline、owner effect、清理失败保留与重试；ACK 和终态收集 | Rust 竞争合同、`runtime_race_contracts.py`、`plugin_bridge_review_contracts.py` |
| R4 | 动态 Pending、出现/撤销/替换、传递子图更新、消费者先清理并保留旧依赖 | Rust 依赖合同、`plugin_host_contracts.py` |
| R5 | 显式父子继承与覆盖、独立服务域、定义与调用 scope、intercept 递归合并 | Rust scope 合同及固定参考轨迹 |
| R6 | emit、parallel、serial、bail、waterfall；注册顺序、重复 next、错误记录与监听器撤销 | `cordis_reference_contracts.py`、`plugin_host_contracts.py` |
| B0 | Platform 和 KnowledgeService 显式接收端口，移除隐式构造与第二份 provider | `platform_ports.py`、`core.py`、`knowledge_service.py`；平台与模型合同 |
| B1 | 移入经核对的 PR #14 模型服务；Rust 路由身份和 owner lease；独立撤销 generation/embedding/OCR | `model_service_contracts.py`、`borrowed_model_contracts.py`、原有物理请求合同 |
| B2 | repository、三种搜索后端、原件和 SessionStore 分别提供服务 | `platform_plugins.py`；真实 EmbeddedSearch 与受控 OpenSearch 使用同一消费者；真实 PG 集成 |
| B3 | 原生 parser 与 OCR 端口；保留解析前授权、解析后锁定与 revision 复查、snapshot 事务 | 真实 HTTP/PG/parser 集成，原有 Markdown、OCR、证据合同 |
| B4 | EmbeddingJobs 和 semantic provider 挂入插件；沿用持久任务状态机和 UNKNOWN 规则 | `embedding_jobs_contracts.py`、真实 `postgres_operations` 恢复测试 |
| B5 | gateway、reranker、检索策略和上下文策略独立装配；BM25-only 可运行 | 平台、默认路径、证据与最终请求绑定合同 |
| B6 | 工具 catalog 和 executor 显式依赖，租约内调用与停止 | `agent_plugin_contracts.py`、`agent_graph_contracts.py` |
| B7 | 通用 Agent 的 loop、models、prompt、context、sessions 和 tools 按 profile 组合 | `harness_profile.py`、`harness_services.py`；通用 Agent、loop、持久会话合同 |
| B8 | 现有 knowledge workflow 和 driver 直接接入；没有新增外层规划模型 | `knowledge_profile_contracts.py`、`knowledge_lifecycle_contracts.py`、冻结默认业务回放 |
| C0 | 根 profile 统一 API、delivery 与运维入口；native、BM25、完整知识库样例 | `application_composition.py`、`config/application-profiles/`、真实 API 单根断言 |
| C1 | 内置 Schema 和纯值预检；引用配置及凭据指纹；相同路径内容变化触发准确更新；失败零变更 | `plugin_configuration_contracts.py` 11 项，Rust 配置指纹合同 |
| C2 | 新运行时成为当前分支唯一默认；旧 Rust host/manifest 与 Python 生命周期裁决删除；同机预算与集成验收 | 正式 checker、[性能报告](rust-plugin-performance.md)、下表本地证据 |
| E0 | 内容寻址安装、来源和字节校验、显式版本选择；独立解释器；握手、调用、取消、退出、崩溃、UNKNOWN | `plugin_process.py`、`scripts/plugin_manager.py`、`examples/process-echo/`；受控进程集成 |
| E1 | 条件扩展，未实施 | 未提出实际不可信或可移植插件需求；不计为已验证能力 |

合同文件均位于统一 `kb_platform/rag_testing/`。模型、Embedding、OCR、检索、会话和任务的领域算法、账本和重试 authority 继续使用现有实现。安装器只接受可信本地包，独立解释器由使用者预先准备，不自动安装依赖。

## 实际验收记录

以下目录均在 `reports/rust-plugin-v1/` 下；旧结果保持只读。每次 checker 重新构建并使用自己的 native 副本，构建失败不回退旧库。

| 范围 | 最终输出目录 | 结果及边界 |
| --- | --- | --- |
| Python 3.12.14 正式入口与受控集成 | `final-check-py312-02` | PASS；455 Python 合同、65 Rust 测试、24 评分器控制样本；零失败、零跳过；进程插件及真实 HTTP/PG/parser 集成通过 |
| Python 3.13.14 正式入口与受控集成 | `final-check-py313-02` | 同上，使用独立 3.13 环境和本轮构建库 |
| PostgreSQL 任务与恢复 | `postgres-acceptance-final-py312-01`、`postgres-acceptance-final-py313-01` | 各 16 项 PASS，合成 Embedding provider，远端调用为零 |
| 当前默认业务图与证据 | `default-business-replay-final-01` | 48/48 PASS；FAIL/ERROR/NOT_RUN/UNCERTAIN 均为零；冻结合成原文、内存检索/存储、脚本模型；当前统一评分，不代表真实 HTTP 或模型质量 |
| 独立插件环境 | `process-independent-final-01` | 3.12 宿主启动真实 3.13 插件，握手、相对导入、取消、deadline、崩溃及单次 UNKNOWN 发送通过 |
| 固定预算与性能 | `acceptance-performance-final-20261030-04/analysis` | 九项冻结绝对预算全部 PASS；五轮 baseline/current 交替进程、五个扩展进程、五个 native 进程；错误为零；233 个源码及三个制品哈希逐字节匹配 |

真实集成使用新建专用数据库 `rust_plugin_acceptance_20261030_205528_e614d1`，没有修改既有 `evidence_kb` 数据库。PostgreSQL 18.6、pg_textsearch 1.5.1、vector 0.8.6。实际 TCP HTTP 验证认证、跨租户隔离和响应头；70 KiB 合成 Markdown 触发一次解析子进程，拒绝请求在解析前终止，72 个证据 span 绑定原文哈希，真实 BM25 build/publish 与引用读取通过。关闭时没有在途操作、租约、资源或残留工作线程。

普通离线合同仍禁止网络和子进程。checker 只在独立受控 fixture 中开放精确命令；`--local-integration` 必须显式提供专用测试数据库。评分器控制样本成绩、合成原文回放、受控组件和真实模型答案质量分别报告。

模型/通用 Agent 的独立检出位于 `.local/rust-models-check`，提交 `c5920ca0d34469a024046350ed1b323fc00e1de8`。该检出中 `models-independent-check-02/summary.json` 的正式 checker 为 PASS，不依赖后续知识库组合源码。第一次隔离检查被会话中断，保留部分输出，未计为通过。

上述本地检查运行于 Windows。五个实施提交均已通过 Ubuntu/Windows × Python 3.12/3.13、secrets 和 required 六项远端检查。最后两个实施提交的证据分别为 [#19 CI](https://github.com/Hubujiu/evidence-kb/actions/runs/41069992320) 和 [#20 CI](https://github.com/Hubujiu/evidence-kb/actions/runs/41070567841)。文档补充提交的最新检查状态以对应 PR 页面为准，本地双 Python 结果不替代远端平台矩阵。

性能的规模 256 热调用 p95 进程中位数从 579.5 μs 到 107.6 μs，当前五个进程范围 100.6–182.5 μs，固定预算 614.2 μs。该结果主要反映桥接和 Windows 动态库路径解析成本。完整业务与 native 新增工作量只有当前结果，不宣称真实 RAG、生产模型或全业务获得相同比例收益。每个扩展进程执行 200 次根创建/关闭循环，八类 Rust 终结计数均为零；五个进程的当前 PrivateUsage 序列均持平。最终测量冻结了包含两处夹具修正的提交 `5d18294`；233 个活动源码、冻结副本及三个制品的原始字节哈希全部匹配，差异列表为空。此前 final-03 及其 parser 空行差异记录保持只读，由 final-04 承担最终验收。

## 保留的失败与合同迁移

- 最初缺 Cargo PATH 的基线失败和源码并行修改导致的构建失败保留；使用独立输出重新检查后通过。
- `final-check-py312-01` 与 `final-check-py313-01` 保留两个过期配置夹具错误。最终仅补齐有效临时配置，资源保留、失败重试及借用对象断言全部保留。
- 首次 PostgreSQL 恢复验收保留 FAIL：旧测试将依赖哈希硬编码成旧版本，实际触发 STALE_BUILD。恢复测试改为当前合法依赖，另增依赖变化必须 STALE_BUILD 且零新增 Embedding 的合同，没有改白名单或门槛。
- 删除旧 `agent-harness/host_tests.rs` 的 13 个测试，是随唯一宿主实现迁移；状态、依赖、租约、scope、卸载及关闭行为由新 Rust coordinator 测试和 22 项 Python host 合同承接。旧“句柄总数持续不变”假设被永不复用身份与终态收集合同替代。C ABI 版本、畸形 JSON、未知字段和大小限制仍有实际测试，没有删除 Python 业务合同。

只读盘点的 412 个历史账本包含副本，其中六个 UNKNOWN 保持原样；不能把副本相加当累计费用。复用了 240 条历史结果、240 个请求绑定、243 个已完成回答的审计材料和锁定来源，没有重发旧物理请求。本轮新增远端模型、Embedding、judge 调用均为零。合成模型调用另计，不是供应商执行。

## 复现

```powershell
$env:PATH = 'C:\Users\Hubujiu\.cargo\bin;' + $env:PATH
uv run --no-sync python scripts/check_project.py --output reports/rust-plugin-v1/new-check
$env:KB_INTEGRATION_DSN = '<专用 rust_plugin_acceptance_ 数据库 DSN>'
uv run --no-sync python scripts/check_project.py --local-integration --output reports/rust-plugin-v1/new-integration
```

性能复现使用已提交的 `kb_platform.rag_testing.harness_performance`，传入冻结 baseline source/library、本轮 release library/native benchmark、`--extended` 及新输出目录。样本、预算、环境、源码和制品哈希见原 `plan.json`、`measurements/manifest.json`，不能以缓存命中或 replay 耗时替代线上时延。

## Attention

reviewed by Codex（独立代理，与实施者同模型族）

跨模型族审计调用被平台的 sealed-task 限制拒绝，不能声称已取得跨模型族复核。独立代理已复核并验证实际竞争问题；其发现对应 owner lease、继承 deadline、路由借用、配置预检和资源回收合同。当前尚未合并；真实付费模型质量、恶意插件隔离、Wasm、全部 npm/TypeScript 兼容和原生动态库热卸载均没有验收结论。
