# PR #17–#20 第二轮修复与验收

本轮处理复核确认的 1 项 P1、7 项 P2。生产源码集成为 `f41f2e718e6ed69cda24e8654d2aecbca432bc81`；后续文档提交不改变被测源码。原始失败与本轮结果分别保存，未修改预算、时限、参考轨迹或通过门槛。未合并 PR。

## 修复与持续回归

合同路径均相对于 `kb_platform/rag_testing/`。

| 编号 | 修复后的行为 | 回归入口 |
| --- | --- | --- |
| 1 · P1 · #19 授权对象被复制 | `validate_request` 继续深复制数据，授权与 validator 能力保留原对象身份。绑定方法、可调用对象在首次检查后撤权，Google `_observed()` 的发送前检查拒绝请求，`_post()` 调用、物理 attempts 和用量均为零。 | `model_review_contracts.py` 的 `test_request_data_copy_preserves_authorization_capability_identity`、`test_observed_pre_send_revocation_uses_original_bound_and_callable_authorizers` |
| 2 · P2 · #17 阻塞 cleanup 延迟取消 | Rust 确认取消后直接投递对应 Event；mount/cleanup 仍由原执行器排序。 | `runtime_race_contracts.py` 的 `test_manual_cancel_notifies_running_call_while_sibling_cleanup_blocks` |
| 3 · P2 · #17 兄弟更新失败销毁正常 scope | 动作失败携带 host/operation，构造器只依据自己的初始化结果清理。A 的失败保留在 A 的 Rust 状态中，B 保持可用。 | 同模块的 `test_sibling_update_failure_does_not_dispose_successfully_mounted_scope` |
| 4 · P2 · #18 空闲进程退出不清理 | reader 在独立退出通知线程中调用 `host.update()`，推进 Python 生命周期执行器。清理不在 reader 中等待 reader 自身。 | `plugin_process_contracts.py` 的 `test_idle_process_exit_pumps_cleanup_outside_reader`；`plugin_process_integration.py` 的 `supervision_cases` |
| 5 · P2 · #18 取消写入失去期限监督 | transport 的一个监督线程覆盖每次写入直到 Future 结束。业务响应不会移除控制写入的期限；已开始的阻塞写入超时后终止进程，未发送队列项单独拒绝。关闭同时等待监督线程结束。 | 同模块的 `test_control_write_deadline_is_supervised_without_pending_business_request`；真实 256 调用拥塞集成 |
| 6 · P2 · #19 明确未发送仍 UNKNOWN | `model_sent=False` 进入 RETRY，attempt 不消耗、attempt 记录为 ERROR、circuit 不增加失败。恢复使用新物理身份；ReadTimeout 继续 UNKNOWN 且不自动重发。 | `model_review_contracts.py` 的离线结算合同与 `live_contract_suite()`；后者已接入 `postgres_operations` |
| 7 · P2 · #20 重排正文重复进入必需状态 | `prepare_rerank()` 在最终序列化前按实际 UTF-8 估算裁剪正文，全部候选的原顺序及 R001…引用保持。最终请求只包含一份候选正文；coverage 引文必须出现在实际发送窗口。 | `reranker_review_contracts.py` 的 `test_large_candidate_turn_preserves_all_positions_within_final_request_budget` |
| 8 · P2 · #20 每轮物理上限被拆分 | 共享 `BudgetLedger` 按 session/turn 保存一份额度，所有 AgentModel 统一预留和结算。并发预留、明确未发送释放、下一轮隔离与额度变更拒绝均有合同。 | `turn_budget_contracts.py` 的四项合同 |

新增回归先在修复前实现复现失败，再对修复执行。第 8 项完整组合运行真实 Luna SDK、重试、上下文、自动格式修复及 SQLite 物理账本，HTTP 供应商边界受控。旧实现以生成 20 次、重排 1 次完成；修复后生成 18 次、重排 1 次，下一次需要两次尝试预留的 verifier 在发送前拒绝，实际共 19 次。该结果验证上限约束，不是模型质量成绩。

第 7 项实际 turn 使用 20 条、每条约 650 个中文字符的合成候选，最终请求在默认 24,000 输入估算预算内发送一次，原始证据不变。两个既有重排合同的响应夹具改从最终 `documents` 读取候选数量，原分值、排序、身份及保留答案/核验额度断言继续保留。

## 本轮实际验收

本地证据根目录为 `reports/rust-plugin-rereview-v2/20261031-01/`。目录及既有失败记录保持只读；Git 交付本摘要及可重跑的合同，原始制品保存在本机。

| 执行 | 结果 | 证据 |
| --- | --- | --- |
| 正式 `check_project.py --local-integration` | 10 个阶段全部 PASS；494 项 Python 合同、65 项 Rust 测试、24/24 评分器控制样本，无失败、错误或跳过 | `final-check/summary.json`、`contracts.json`、`rust-tests.log`、`scorer-controls/report.json` |
| 真实进程插件 | 空闲退出自动清理；默认 OS 管道下 256 个业务结果返回后，剩余取消写入仍被终止监督，无存活 writer/子进程；原 1 MiB deadline、cancel、排队超时仍通过 | `final-check/plugin-process-integration/result.json` |
| HTTP 与数据库 E2E | 2/2 PASS：真实 loopback TCP、认证中间件、解析子进程、隔离 PostgreSQL、构建/发布/检索/引用和 ACL；关闭后资源与租约为零 | `final-check/runtime-integration/report.json` |
| PostgreSQL 恢复专项 | 21/21 PASS，使用 `kb_app` 与真实 RLS；覆盖三类连接错误两次失败后恢复、ReadTimeout UNKNOWN、已有 capture 恢复与构建恢复 | `postgres-operations/report.json` |
| Cordis 重新下载执行 | 9/9 固定来源场景通过；新参考输出与锁定轨迹解析后完全相同；当前 Rust/Python 对照合同随 494 项合同通过 | `cordis-traces.json`、`verification.json` |

复跑正式入口需设置 `KB_INTEGRATION_DSN`；恢复专项使用同一专用库的 `KB_DSN`，并令 `KB_AGENT_KERNEL_LIBRARY` 指向本轮正式入口产出的 native 副本。数据库名须为新的 `rust_plugin_acceptance_*`，地址为本机 `127.0.0.1:55432`。初始化与环境要求见 [开发约定](development.md)。

```powershell
uv run --no-sync python scripts/check_project.py --local-integration --output reports/ci/<新的目录>
uv run --no-sync python -m kb_platform.rag_testing.postgres_operations --output reports/ci/<新的恢复目录>
uv run --no-sync python scripts/generate_cordis_reference.py --workdir .local/<新的源码目录> --output reports/ci/<新的参考目录>/traces.json --download
```

## Cordis 对照的范围

本轮重新下载并校验 [DeepSeek Harness ce7073d](https://github.com/deepseek-ai/deepseek-harness/tree/ce7073d51254c26cd63b22a60835b8f948deb447) 中锁定的 20 个 Cordis/Cosmokit 原始文件，使用 Node `v22.23.1` 执行未修改源码。来源、依赖和生成器哈希仍由 `config/plugin-runtime/cordis-reference-lock.json` 固定，没有改写参考金标。

九个场景比较依赖启动/清理、动态依赖、scope 覆盖与父资源借用、事件监听归属、parallel 偏序、serial/bail 终止和 waterfall 委派。清理失败保留资源、同步 Python 取消只通知等有意差异继续见 [来源与差异](rust-plugin-provenance.md)。本结果不覆盖全部 Cordis API，也不表示运行了整个 DeepSeek Harness 应用或完整上游测试集。

## 分层交付与边界

#17 为 `f93ad08`，#18 为 `64e6bca`，#19 为 `d52c643`；下游通过正常 merge 包含上游修复，没有强制推送。#20 在 `f41f2e7` 集成上述源码与自己的 `1e163a2`。合并 `postgres_operations` 时，上游新增的“搜索依赖改变拒绝恢复”与 #20 已有同行为合同重复；保留原 `test_changed_search_dependency_rejects_resume_without_new_embedding`，吸收 retry_at 与完整调用列表断言，未删除原行为覆盖。

所有数据为合成夹具，模型边界受控。新增真实供应商模型、Embedding 和 judge 请求均为零。旧付费 trace、裁判结果和性能测量保持只读；本轮未重复付费链路，也未测量或宣称性能提升。远端 CI 以各 PR 最新 HEAD 的实际 Actions 结果为准，不能用本地通过替代。
