# Rust 插件审查修复与证据

用户复核确认了八项缺陷，其中三项 P1、五项 P2。审查前 `105d664` 的完成结论因此失效。原有 455 项 Python 合同、65 项 Rust 测试、24 个评分器控制样本及 final-04 性能结果仍是历史执行证据，不能证明这些缺陷不存在，也不能作为修复后版本的性能结论。

本文核对的生产源码为 `6fd9ab8a57ed19c420220327769beec1712f7e62`。修复期间新增合同先在原实现复现失败，再对实际修复执行检查。原始失败目录保持只读，未删除失败样本、修改验收门槛或重写参考金标。正式全量检查、隔离分支检查、远端 CI 和当前性能的状态分别记录在文末。

## 八项问题与回归

表中合同均位于 `kb_platform/rag_testing/`。`runtime_review_contracts.py` 的测试类为 `RuntimeReviewContracts`，`model_review_contracts.py` 的离线测试类为 `ModelReviewContracts`。

| 用户编号与问题 | 修复后的行为 | 新增回归与修复前后证据 |
| --- | --- | --- |
| 1 · P1 · #17 热更新遇到在途调用后永久 `cleaning` | 调用完成、scope 租约或 owner 租约释放后，桥接自动继续根生命周期动作。根只允许一个动作执行者，业务回调在元数据锁外执行。无需第二次管理调用，新配置即进入 `ready`。 | `runtime_review_contracts.py` 的 `test_operation_completion_drives_pending_update_without_a_second_management_call`、`test_scope_lease_release_drives_pending_update_without_a_second_management_call`、`test_owner_lease_release_drives_pending_update_without_a_second_management_call`。修复前 [5 项失败日志](../reports/rust-plugin-review-v1/runtime-before-20261030-231927-1211fd/checks.log)含三个 `cleaning != ready`。修复后 [46 项集中检查](../reports/rust-plugin-review-v1/runtime-focused-20261030-232728-e94409/report.json)通过。 |
| 2 · P2 · #17 子作用域调用父服务丢失 deadline 和准入 | 父服务的 owner 租约继承同线程、同实例和同代次的实际调用准入及原 deadline。已准入调用可在父 scope 开始排空后完成。复制到其他线程的上下文和旧代次不能借用准入。 | `test_inherited_owner_lease_keeps_admitted_deadline_and_can_finish_after_parent_drain`、`test_prior_generation_owner_cannot_borrow_a_current_inherited_call`、`test_copied_call_context_cannot_borrow_admission_in_another_thread_after_drain`、`test_nested_serial_owner_leases_keep_the_current_call_chain`。修复前 [deadline 丢失及 draining 拒绝](../reports/rust-plugin-review-v1/runtime-before-20261030-231927-1211fd/checks.log)。修复后同一 [46 项检查](../reports/rust-plugin-review-v1/runtime-focused-20261030-232728-e94409/report.json)通过。 |
| 3 · P1 · #18 同步写进程管道使 timeout 和 cancel 失效 | 单独的 writer 线程串行写入。调用使用一个绝对期限覆盖排队、写入、响应和控制消息。阻塞时宿主终止并等待子进程，随后等待 writer 结束，再释放 Rust 租约。cancel 消息仅含协议和身份控制字段。排队但未发送的请求超时不终止先前调用。 | `plugin_process_integration.py` 的 `blocked_pipe_cases` 使用真实子进程。子进程完成握手后停止读 stdin，宿主发送 1 MiB 请求。修复前 [deadline 与取消均需 1.5 秒外部 watchdog](../reports/rust-plugin-v1/process-review-before-pipe-02/result.json)。修复后 [同版本子进程](../reports/rust-plugin-v1/process-review-after-full-05/result.json)及 [3.12 宿主与 3.13 子进程](../reports/rust-plugin-v1/process-review-after-cross-python-04/result.json)的三种场景均通过。每个实际发送的阻塞请求只有一次 SENT、一次 UNKNOWN，无重发、存活子进程、writer 或残留租约。合法 1 MiB echo 仍通过。 |
| 4 · P2 · #18 新增 helper 包绕过完整性，catalog 后挂载不复查 | 安装记录整个包的相对路径及内容哈希，catalog 和每次 mount 都比较完整清单。新增、删除、内容变化、符号链接和 junction 均拒绝。安装时排除源缓存及字节码，已安装包出现缓存或字节码即拒绝；子进程使用 `-B`。相对导入仍可正常运行。 | `plugin_process_contracts.py` 的 `test_added_package_can_not_shadow_pinned_helper_module`、`test_mount_rechecks_files_changed_after_catalog_creation`、`test_source_bytecode_is_not_installed_and_installed_bytecode_is_rejected`。修复前两项真实失败见[保留的工具输出](#包完整性修复前的实际输出)。修复后 [9 项合同及源码哈希](../reports/rust-plugin-v1/process-review-after-full-05/review-manifest.json)通过，受控进程安装、启停与 echo 见同目录 `result.json`。 |
| 5 · P1 · #19 账本 producer BUSY 或 SEALED 在发送前拒绝，却变成 UNKNOWN | 物理账本拒绝保留明确的 preflight 原因和 `model_sent=False`，不创建请求 capture。Embedding job 保持 `RETRY`、attempt 0，熔断失败数不增加。BUSY 解除后同任务可以继续。 | `test_real_producer_busy_and_sealed_are_zero_effect_model_preflight`、`test_observed_port_retains_producer_preflight_classification`，以及真实 PostgreSQL 的 `test_producer_busy_is_retry_without_attempt_or_circuit_failure_and_resumes`、`test_sealed_producer_stays_retry_without_physical_or_circuit_effects`。修复前 [离线失败](../reports/rust-plugin-review-v1/model-before-20261030-02/contracts.log)、[observed 端口失败](../reports/rust-plugin-review-v1/model-observed-preflight-before-20261030-01/contracts.log)及 [数据库失败](../reports/rust-plugin-review-v1/model-live-before-20261030-01/contracts.log)保留。修复后 [63 项集中合同](../reports/rust-plugin-review-v1/model-final-focused-20261030-01/contracts.log)和 [14 项数据库合同](../reports/rust-plugin-review-v1/model-final-live-20261030-01/contracts.log)通过。 |
| 6 · P2 · #19 `ConnectError`、`ConnectTimeout`、`PoolTimeout` 明确未发送，仍扣上层调用与 token | 三种已知未发送错误保留 `model_sent=False` 和 attempts 0。物理账本与 Agent、turn 两层预算释放相应预留。相同额度下后续成功调用仍能执行。实际发送后结果未知的规则不变。 | `test_known_unsent_transport_releases_physical_and_upper_turn_budgets` 用本地 `httpx.MockTransport` 逐一触发三种错误，分别检查两次失败后预算为零，再执行成功调用。修复前 [三个子场景失败](../reports/rust-plugin-review-v1/model-before-20261030-02/contracts.log)。修复后 [63 项集中合同](../reports/rust-plugin-review-v1/model-final-focused-20261030-01/contracts.log)通过。 |
| 7 · P2 · #19 Embedding 包装丢失 `request_id`、`audit_context`、`authorize` 及 capture 恢复端口 | 显式 `ObservedEmbeddingPort` 保留请求身份、审计上下文、授权回调和原 provider manifest。恢复通过同一受租约约束的路由读取已结算 capture，并核对 payload、原文、向量哈希和物理账本状态。旧绑定、篡改记录和授权失败均拒绝，不重发。普通仅接收 texts 的适配器仍支持。 | `test_observed_documents_keep_raw_identity_audit_authorization_and_manifest`、`test_observed_query_keeps_explicit_request_context`、`test_observed_recovery_uses_settled_capture_without_resend_and_rejects_stale_route`、`test_plain_google_texts_only_adapter_stays_supported`，以及数据库 `test_observed_complete_capture_reconciles_crash_without_resend`。修复前 [包装及恢复失败](../reports/rust-plugin-review-v1/model-before-20261030-02/contracts.log)与 [数据库恢复失败](../reports/rust-plugin-review-v1/model-live-before-20261030-01/contracts.log)。修复后 [63 项集中合同](../reports/rust-plugin-review-v1/model-final-focused-20261030-01/contracts.log)及 [14 项数据库合同](../reports/rust-plugin-review-v1/model-final-live-20261030-01/contracts.log)通过。 |
| 8 · P2 · #20 `KnowledgeTurn` 固定 magpie_luna 重排器覆盖注入的 vendor-a 或 none，退回 BM25 | turn 使用已配置的 reranker。注入 vendor-a 时保留 provider 身份及 BM25、dense 两个渠道；none 保持关闭。需要模型的既有重排算法通过 turn completion 回调执行，实际选中模型的调用进入同一预算和 capture。 | `reranker_review_contracts.py` 的 `test_actual_turn_uses_injected_vendor_reranker_and_hybrid_search`、`test_disabled_reranker_stays_disabled_during_actual_turn_with_legacy_client`、`test_turn_accounts_actual_selected_model_without_replacing_rank_algorithm`。修复前 [3 项均失败](../reports/rust-plugin-review-v1/reranker-before-02/contracts.log)。修复后 [含相关旧合同的 31 项检查](../reports/rust-plugin-review-v1/reranker-after-01/contracts.log)通过。 |

## 修复中发现的两项缺陷

这两项属于修复过程发现的附加问题，不计入用户的八项问题。

| 问题 | 修复与合同 | 证据 |
| --- | --- | --- |
| A · owner 租约退出先触发自动清理，模型 active 尚未减一，导致 `MODEL_BUSY` 和 `cleanup_failed` | `ModelService.lease` 先取得 owner 准入，再以不可变 registration identity 复核路由，最后登记领域 active。退出时先减 active，再退出 owner 租约。owner 准入失败、期间替换或 adopt 都不执行 adapter、不泄漏 active。合同为 `test_raw_bound_invocation_releases_domain_use_before_owner_cleanup_pump` 和 `test_owner_admission_failure_and_route_change_do_not_leak_domain_use`。 | [原清理顺序失败](../reports/rust-plugin-review-v1/model-owner-before-20261030-01/contracts.log)、[adopt 竞争失败](../reports/rust-plugin-review-v1/model-adoption-race-before-20261030-01/contracts.log)均保留。最终 [63 项检查](../reports/rust-plugin-review-v1/model-final-focused-20261030-01/contracts.log)通过。 |
| B · 根动作执行者忙碌时，子 scope 构造可返回 Starting，并把挂载失败归到其他调用 | 构造在 native create 前保留根执行权。根忙碌时同步报 `PLUGIN_LIFECYCLE_BUSY`，不创建子 scope，也不运行 mount。native 创建拒绝会释放保留；之后合法构造仍可成功。合同为 `test_scope_constructor_rejects_busy_root_before_creating_a_child_or_running_mount` 和 `test_native_creation_rejection_releases_constructor_reservation`。 | [修复前未拒绝 busy 构造](../reports/rust-plugin-review-v1/runtime-constructor-before-20261030-233542-84d3ef/checks.log)。修复后 [48 项检查](../reports/rust-plugin-review-v1/runtime-constructor-final-20261030-233724-10c066/checks.log)通过。 |

## Cordis 并发夹具的因果条件

`test_parallel_partial_order_matches_reference` 原夹具依赖两个 Python 线程的启动调度顺序。`089ca00` 只增加 `first_started` 屏障，让第二个监听器等到第一个记录 `a.start` 后再继续。第一个仍等待第二个完成，故测试仍能识别串行实现。`assertCountEqual` 和参考轨迹中全部 `required_before` 断言保持原样，固定的 Cordis 源码、锁文件和金标轨迹未变。

[原始分层检查失败](../.local/rust-review-process/reports/rust-plugin-review-v1/check-process-01/contracts-3/contracts.log)保留 `AssertionError: 2 not less than 0`。后续 `check-process-02` 及最终双 Python 检查通过，不能覆盖为首轮通过。修正提交也移入 #17，为 `f1c313b`，该提交的最新 CI 已通过。

## 重排端口作者合同

自定义 `rank(query, candidates)` 保留自己的算法和 provider metadata。需要 turn 模型记账的端口可实现 `bind_turn(completion)`，返回绑定本次 turn 的副本，不能修改全局共享实例。`completion(model, system, user, purpose)` 必须传实际选中的 structured model，调用经 `KnowledgeModelCalls.call` 进入 turn 预算、最终请求采集和 capture。`LunaCandidateReranker` 与 `TextView` 已实现该合同。第三方端口的内部外部调用不因存在 `rank` 方法而自动取得完整记账证明。

原有两项重排合同改为显式安装实际候选重排器，并提供该插件要求的 coverage 响应夹具。保留预算余量、候选身份和全部非法分值断言；返回顺序按实际插件的分值排序合同核对，不再依赖 turn 内固定实现。

## 包完整性修复前的实际输出

以下是本会话实际失败输出的摘录，由 `rtk recall 0a4a8c685b512711` 恢复。它是保留的工具输出，不冒充另一次执行或已有 JSON 报告。

```text
FAIL: test_added_package_can_not_shadow_pinned_helper_module
AssertionError: PluginError not raised

FAIL: test_mount_rechecks_files_changed_after_catalog_creation
  File "kb_platform/plugin_process.py", line 101, in mount
    transport.start()
AssertionError: changed code must not execute

Ran 4 tests in 0.101s
FAILED (failures=2)
```

## 提交与分层交付

以下为累计分支中的实际源码提交，代码差异可用 `git diff 105d664..6fd9ab8` 重查。分支合并导致同一修复也有移植提交，不能按提交数重复计算修复数量。后续证据文档提交不改变本次测量的生产源码。

| 修复 | 原修复提交 | 分层 PR 当前提交 |
| --- | --- | --- |
| 运行时问题 1、2 | `af09b42` | #17 包含 `65e8434` |
| 子 scope 构造问题 B | `4253304` | #17 包含 `25bc017` |
| Cordis 因果夹具 | `089ca00` | [#17](https://github.com/Hubujiu/evidence-kb/pull/17) 为 `f1c313b` |
| 进程问题 3、4 | `ce15827` | [#18](https://github.com/Hubujiu/evidence-kb/pull/18) 为 `fb7a511`，包含移植修复 `de12458` |
| 模型问题 5、6、7 和问题 A | `83a2b1e` | [#19](https://github.com/Hubujiu/evidence-kb/pull/19) 为 `c513cfd` |
| 重排问题 8 | `e050fee` | [#20](https://github.com/Hubujiu/evidence-kb/pull/20) 为 `6fd9ab8` |

## 当前验收与计划指标

下表区分已核对的集中回归与待补的最终验收。`PENDING` 不计为 PASS。原有验收和 final-04 性能文档保留原始数字，适用版本仍为审查前 `105d664` 所指实现及性能冻结的 `5d18294`。

| 验证范围或指标 | 当前记录 | 证据与待完成项 |
| --- | --- | --- |
| 八项问题及两项附加缺陷 | 上文列出的集中检查 PASS | 运行时 46 项、构造 48 项、进程 9 项及真实子进程、模型 63 项与数据库 14 项、重排 31 项分别是不同检查集合，不能相加当独立合同总数。 |
| Python 3.12 最终正式入口 | PASS，十个阶段均退出 0 | [summary.json](../reports/rust-plugin-review-v1/final-check-py312-01/summary.json)及同目录 `contracts.json`。482 项 Python 合同、65 项 Rust 测试、24 个 scorer controls 均通过，零失败、错误或跳过；受控进程及本地 HTTP、parser、PostgreSQL 集成通过。使用本次独立构建的 native 制品。 |
| Python 3.13 最终正式入口 | PASS，十个阶段均退出 0 | [summary.json](../reports/rust-plugin-review-v1/final-check-py313-01/summary.json)及同目录 `contracts.json`。同样为 482/65/24，零失败、错误或跳过，进程及本地集成通过。使用该输出目录独立构建的 native 制品。 |
| #17、#18 隔离分层检查 | PASS | [core 408 项合同](../.local/rust-review-core/reports/rust-plugin-review-v1/check-core-02/summary.json)、[process 417 项合同](../.local/rust-review-process/reports/rust-plugin-review-v1/check-process-02/summary.json)及各自同目录 `contracts.json`。之后仅补 Cordis 夹具因果屏障，最新 #17、#18 CI 覆盖该变化。 |
| #19 隔离分层检查 | PASS，458 项 Python 合同、65 项 Rust 测试、24 个 controls | [check-models-01](../.local/rust-models-check/reports/rust-plugin-review-v1/check-models-01/summary.json)及同目录 `contracts.json`。零失败、错误或跳过，不依赖最终知识库根组合。 |
| 当前默认业务回放 | PASS，48/48 | [统一评分结果](../reports/rust-plugin-review-v1/default-business-replay-01/summary.json)中 FAIL、ERROR、NOT_RUN、UNCERTAIN 均为零；执行当前默认图，复用冻结合成原文，使用内存检索/存储及脚本模型，不是供应商质量或线上延迟。 |
| 真实 PostgreSQL 恢复 | 两个 Python 版本各 16 项 PASS | [3.12](../reports/rust-plugin-review-v1/postgres-final-py312-01/report.json)、[3.13](../reports/rust-plugin-review-v1/postgres-final-py313-01/report.json)。与新增三项模型任务数据库回归分开报告，不能相加当独立用例总数；没有远端调用。 |
| Ubuntu、Windows 与 Python 3.12、3.13 CI | #17、#18、#19 SUCCESS；#20 最终文档提交结果见 PR 检查页 | 已核对 [#17 f1c313b](https://github.com/Hubujiu/evidence-kb/actions/runs/41083198853)、[#18 fb7a511](https://github.com/Hubujiu/evidence-kb/actions/runs/41083203779)、[#19 c513cfd](https://github.com/Hubujiu/evidence-kb/actions/runs/41083215854)。[#20 6fd9ab8 首轮](https://github.com/Hubujiu/evidence-kb/actions/runs/41083274311)的 Windows 3.12 三个合同分片超过固定 170 秒，只有 121 个合同完成，记录为 FAIL（3 ERROR，discovery_complete=false）；没有断言失败。其他三个平台组合、Rust、进程集成和 scorer controls 通过。原[失败制品](../reports/rust-plugin-review-v1/ci20-failure-41083274311/evidence/contracts.json)保留；文档提交重新运行同一完整门禁，不修改超时、样本或失败处理，结果以 [#20 最新检查](https://github.com/Hubujiu/evidence-kb/pull/20/checks)为准。 |
| 九项冻结性能预算 | PASS | 修复后源码 `6fd9ab8` 使用新 checker 构建 DLL，五轮交替配对的全部当前进程 p95 均满足原九项绝对预算。[当前性能报告](rust-plugin-review-performance.md)与[原始分析](../reports/rust-plugin-review-v1/performance-final-01/analysis/analysis.json)保存实际工作量、误差范围、环境和哈希；旧 final-04 保留为历史记录。 |
| 扩展工作量、资源残留与内存 | PASS，限定为合成工作量 | 五个 native 进程各执行 32 项组合；每个扩展进程完成 357 次关闭回调及 48 次合成模型调用，八类终态残留均为零。五个 200 次根循环的当前 PrivateUsage 序列均持平，句柄数 199、Python 线程数 1。236 个当前源码、197 个基线文件及三个制品逐字节核验通过，差异为空。新增完整业务只有当前测量，不宣称生产 RAG 提速。 |
| 新增供应商模型、Embedding、judge 调用 | 已核对的本地回归均为 0 | 物理账本使用真实 SQLite，集成使用本地 PostgreSQL 与 MockTransport 或受控本地子进程。合成调用不是供应商调用。后续检查仍需以各自实际记录为准。 |

## Attention

reviewed by Codex（独立代理，与实施者同模型族）

同模型族独立代理已复核生命周期、进程插件和模型/实际问答路径，并分别执行相关合同；67 项模型与重排合同通过，真实阻塞管道及完整性检查通过。最终独立证据审计核对了本页本地引用、日志列数、各版本测试数量、性能原始工作量及源码与制品哈希，未发现与实际记录不符的结论。`reports/` 和 `.local/` 下的原始制品仅保存在本机，不随 Git 文档提交。本轮再次尝试 Gemini 3.8 Flash 跨模型族审计，任务被平台 sealed-task 限制拒绝，未产生审阅结论。原始失败和历史性能版本边界在本文保留；PR 尚未合并。
