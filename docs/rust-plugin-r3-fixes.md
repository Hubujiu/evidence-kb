# 第三轮审查修复与验证

本轮处理审计包确认的九项 P2、独立 #14 的三项遗留问题，以及 #1/#18/#19 的门禁问题。未合并 PR，未运行真实供应商模型、Embedding 或 judge。当前整栈生产源码为 `39476165090d0491ae7868a0016a325e525d76e3`；其文件内容与完成 Python 3.12 验证的 `baa7b1d` 相同。

原 main 的 M1/M2/M4 单列为[草稿 PR #21](https://github.com/Hubujiu/evidence-kb/pull/21)，基于 `39199c0`，并已正常合入 #14 和 #15–#20。#14 继续保持独立分支，未引入 Rust 插件重构。

## 九项修复

合同位置均相对于 `kb_platform/rag_testing/`。新增反例先验证原实现失败，再验证修复；保留旧问题已通过的回归。

| 审核编号 | 修复与保留的不变量 | 持续验证 |
| --- | --- | --- |
| 1 / M1 · 冲突 caps 并发初始化 | 构造器在 `BEGIN IMMEDIATE` 后读取、比较并写入冻结配置。成功实例采用数据库确认值，并与调用方的可变字典分离。不同 caps 不能同时被接受。 | `ledger_race_contracts.py`：真实 SQLite、两个构造线程、受控读写交错；原实现两个冲突实例均成功，修复后第二个拒绝。 |
| 2 / M2 · Google UNKNOWN 准入交错 | `reserve(require_settled=True)` 在同一写事务检查未决请求并预留。原 summary 仍可快速拒绝，但不再是准入权威。 | 同模块：在真实 summary 返回后插入 UNKNOWN，数值预算仍有余量；修复前进入受控发送，修复后零发送、无新增请求。 |
| 3 / M4 · 过期未发送结果困在 UNKNOWN | 同一 request_id 的已知未发送结果可从过期 RUNNING 或 UNKNOWN 转 RETRY，退回 attempt、不增加 circuit。恢复还可核验匹配的 ERROR capture、物理账本零用量与零发送次数。旧 request_id、发送不明及不匹配证据不能写回。 | `embedding_jobs_contracts.live_contract_suite()` 新增三项，执行真实 PostgreSQL SQL、kb_app/RLS；覆盖先 reconcile 再返回、后续新 claim、旧 worker、损坏/不匹配证据和零用量恢复。 |
| 4 / L1 · update 缓存反序 | 锁外执行配置校验/输入回调；锁内重验配置快照，串行完成 native 提交与 `_entries` 回写。回调期间配置变化会重新计算输入。 | `runtime_race_contracts.py` 的并发 update 与 configuration callback 合同；真实 Rust、线程屏障，随后停用/启用仍成功。 |
| 5 / L2 · Event 注册前取消被丢弃 | native 准入与 Python Event 发布相对动作执行器使用同一元数据锁。已消费的取消始终可到达其调用。实际工作返回前仍持有租约。 | 同模块的 `test_update_cancellation_cannot_be_consumed_before_signal_registration`；真实 Rust 动作与业务 Event。 |
| 6 / L3 · 初始化异常遗留准入 | 准入后立即建立 finally 保护，按实际初始化进度清理 Event、Timer、ContextVar、operation 和 lease。 | 同模块的 `test_call_initialization_failures_release_admission_and_cancellation_state`；覆盖 Event、Timer.start、上下文初始化故障，业务零执行、终态资源归零。 |
| 7 / R1 · 旧退出通知关闭新代 | Rust 的 `runtime_instance_exited` 原子匹配 plugin/instance/generation 和当前状态。异步通知不再按稳定 plugin ID 无条件停用。 | Rust coordinator 测试、`plugin_process_contracts.py` 代次合同，以及真实 OS 子进程延迟旧通知 E2E；放行旧通知后新进程仍 ready 且可调用。 |
| 8 / R2 · 安装元数据半写发布 | 临时元数据完整写入、fsync、读取验证后原子 replace；最后切换 active。重试校验已有记录，损坏记录明确拒绝；catalog 只读取 active 指针指向的完整安装。 | 同模块的中断发布与损坏重试合同，真实文件系统；旧正常包继续可读取，不把 exists 当作成功。 |
| 9 / M3 · 审计二次异常丢发送事实 | 发送状态由外层独立变量保护。ledger.finish 或 save 再次失败时，最终异常仍携带发送事实；上层实际发送计数不会释放为零。已知未发送仍零结算。 | `model_http_failure_contracts.py` 三项、六种组合：真实 SQLite trigger 故障、真实目录被文件替代，覆盖发送后超时、连接失败和已收到响应；执行实际 AgentModel 结算。 |

M4 的快速租约测试采用受支持的 1 秒配置，并由真实 PostgreSQL 将 lease_until 置于过去，控制交错；不把它描述成默认 300 秒配置下的一次普通 120 秒 HTTP timeout。恢复始终需要当前权限与请求身份，无可靠未发送证据的 UNKNOWN 继续保留。

## 独立 #14

#14 的模型包装完整转发 authorize、audit_context、request_id，保留授权与 validator 能力身份，提供 observed Embedding 的请求恢复端口。ConnectError、ConnectTimeout、PoolTimeout 明确未发送时零用量、RETRY/attempt 0/circuit 0；ReadTimeout 继续 UNKNOWN。M3 与共享账本/租约修复均已独立合入。

`model_embedding_review_contracts.py` 验证标准 View → ModelService → Adapter → Google 链，不依赖显式旁路。其四项 live 合同已接入 #14 的 `postgres_operations`。最终 #14 代码树的正式检查为 387 项合同通过；真实 PostgreSQL 官方验收 22/22，通过绑定/可调用授权撤销、请求与审计身份、已知未发送、过期恢复和未知结果隔离。

## CI 问题的处理

- #1 的原失败是 `10.000000000000028 <= 10`，工件上传成功。本轮采用与相邻合同相同的固定时钟方法：从 100 秒开始、发送前推进到 101 秒，验证 SDK 收到准确剩余 9 秒；保留正数、父截止与过期零发送断言，没有扩大超时容差。
- #18 的 20ms 场景接受并验证合法发送前取消：SENT=0、无 UNKNOWN、无租约残留，transport 后续 echo 正常。发送后路径使用明确屏障验证。E2E 关闭仅对合同允许的 busy 状态有界等待，仍要求最终 disposed、零资源和线程退出；没有改变产品 close 语义。
- #19 补入现有逐题 wall/CPU 和 150 秒 faulthandler 线程栈记录，170/180 秒门槛、四分片、发现集合和失败语义不变。旧 `d52c643` 加诊断在隔离 Windows Python 3.13.14 上正式 465 项合同通过，未复现超时；原 CI 是 Python 3.13.16/Windows Server 2025，现有证据不能确定原超时根因。
- #17 原 required 的日志步骤成功但 GitHub job 最终被取消；没有跨 PR concurrency 冲突证据，未修改 workflow。新 HEAD 已重新触发完整运行。相同 HEAD 的重复触发可能有 cancelled 记录，验收以实际 PR checks 和 completed required 为准。

正常构建恢复夹具曾篡改新建 build 的依赖哈希为历史常量。修复夹具使正常恢复使用它实际冻结的依赖，同时单独保留“依赖改变 → STALE_BUILD、零新增 Embedding”合同。跨分支合并时统一两个同义测试名称，完整保留恢复、不重发、拒绝变更与发布断言；没有修改生产依赖检查或历史 hash 放行规则。

## 本轮执行结果

整栈证据根目录为 `reports/r3-repair-20261031-01/`。原始审计包解压件保持只读，复跑输出另存。

| 范围 | 实际结果 | 证据 |
| --- | --- | --- |
| 整栈 Python 3.12 正式入口，含 local integration | PASS；507 项合同、66 项 Rust 测试、24/24 scorer controls，零失败、错误或跳过 | `stack-py312/summary.json`、`contracts.json`、`rust-tests.log` |
| 整栈 Python 3.13 正式入口，含 local integration | 同上全部 PASS；使用独立 3.13 环境 | `stack-py313/summary.json`、`contracts.json` |
| 真实 HTTP/parser/PostgreSQL E2E | 两个版本各 2/2 PASS；包含认证、ACL、构建/发布/检索/引用、关闭资源归零 | 两个目录的 `runtime-integration/report.json` |
| 真实进程插件 E2E | 两个版本均 PASS；延迟旧代通知、发送前取消、原 1 MiB 阻塞路径和 256 次业务完成后的控制写监督继续通过 | 两个目录的 `plugin-process-integration/result.json` |
| 整栈官方 PostgreSQL 恢复入口 | 24/24 PASS，使用真实 kb_app/RLS | `stack-postgres-operations/report.json` |
| 共享 main 修复 | 正式 346 项合同及其他门禁 PASS；官方 PostgreSQL 恢复 18/18 PASS | `.local/r3-shared-main/.local/r3-main-final-check-01/`、`r3-main-postgres-01/` |
| 独立 #14 | 正式 387 项合同及其他门禁 PASS；官方 PG 22/22 PASS | `.local/r3-model14-check/.local/checks/fc9d8330f60a44a298d91ac4a2fbba05/` 与同 worktree 的 `r3-postgres-operations-6d2c3fc56bf940bca881700e03624600/` |
| 原审计有限模型复跑 | 25 模型、5,003 状态、10,411 转移，与包内统计一致，包含历史缺陷与建议修复对照 | `model-replay/model_summary.json` |

有限模型复跑验证审计材料可重现，不是修改后系统的形式证明。修复验收由真实函数、Rust、OS 子进程、文件系统、SQLite、PostgreSQL 与正式合同提供；未运行 TLC，未宣称任意调度下的活性或完整系统正确性。

本轮新增真实模型、Embedding、judge 调用均为零；旧结果、未知预留与费用账本未重写。没有性能提升或付费节省结论。没有需要业务决策而暂缓的修复项；历史损坏安装记录自动修复/删除未扩展，仍明确拒绝其激活。

远端 CI 结果单独以各 PR 当前 HEAD 的 Actions/checks 为准，不用本地 PASS 覆盖历史 FAIL、CANCELLED 或尚未完成的运行。所有 PR 均保持未合并。
