# API 交付的模型与源码审查

本轮先审查模型、假设和实际回归，再更新列出的源码绑定。摘要只表示这些源码已重新审查，不是模型精化证明。

## 文档版本与权限

`Platform._source_snapshot(published=True)` 从当前 manifest 的冻结 sources 选择版本与解析身份，同时继续使用当前 ACTIVE 绑定、来源用途策略、读者与租户 authority revision。解析数据读取前后仍检查同一 authority、KB revision 与 manifest。`Platform.search` 与 `EvidenceGateway._state` 使用这个发布视图；stage/resume 继续使用候选绑定。候选更新不改变已发布原文，撤权和移除仍即时阻断读取。

`Platform.ingest`、bind、remove、policy 的新 expected_revision 只加强既有事务内守卫。ingest 在解析前及提交事务中复核，reparse 读取不可变原件后调用同一 ingest。authority_publication 与 knowledge_atomic_commit 的版本锁、发布指针与撤权抽象保持适用。ingestion_migration 的来源更新身份与迁移隔离没有改变。knowledge_sql 新增的是只读来源元数据查询，Embedding jobs 的转换没有修改。

`Platform.documents` 是读取与最终 revision 复核，不持久化就绪状态。API 文档生命周期合同使用真实权威存储验证授权、PARTIAL 处理、条件更新、发布前后版本、模型实际输入及引用原件。任意外部存储的事务保证、无限文档规模、响应送达瞬间的撤权与分布式原子性仍不在有限模型证明内。

## 模型调用

原 invoke 中实际状态与发送、结算转换移入 `_invoke_admitted`，invoke 持有已有 producer guard。注册新 owner 锚点及其 preserve 事实投影，保留旧 invoke 锚点。403 先持久阻断再结算；一个物理请求最多调用一次 finish。响应状态与已观察用量先采集，捕获记录与账本提交确认分别保存。Anthropic 缺失用量坐标保留未知。释放 producer guard 出错时保留原调用异常。

PhysicalBudget、ObservedSettlementFacts 与 PermissionLatch 的单次发送、结算至多一次、未知预留、已观察用量不丢失及权限阻断假设未放宽。AgentModel.complete 只把新增事实纳入已有失败 call_record，不改变 turn 预算状态转换。model_errors 的模块级字段映射增加观测坐标。现有多个模型族引用这些同一源码，因此逐个更新对应绑定。

`_HttpCompletion` 为冻结 dataclass 的响应观测值，HTTP status 不拥有可变生命周期；作为发现器假阳性明确排除。网络崩溃、文件系统全部失效、并行多宿主公平性及每个适配器至 HTTP 用户端的完整精化仍未证明，由故障注入合同补充，不能称为形式化全覆盖。

## 受限恢复

原 BatchSealing 仍描述可写 child 的前序封存与预算规则。LinkedBudgetLedger 的显式恢复读取可以保留未知 child 并返回对象，但 admission、reserve、finish 永久拒绝。默认调用方的拒绝语义、前序身份、兼容 successor、重复物理身份及费用校验不变。

新增 BatchRecovery 有限切片描述恢复进程：一个不可变 UNKNOWN child、一份保留预留、授权或未授权读者和有界发送计数。Read 对应既有身份和当前授权检查；Reserve/Finish 对应恢复账本拒绝；Send 对应 HTTP 入口与 transport 阻断。正例检查 RecoveryReadOnly、AuthorizedRead；两个负控分别放开写入及绕过授权，必须违反指定不变量。这个抽象把认证、路由选择与阻断实现当作假设，实际 HTTP 重启合同检查其装配，并不证明 FastAPI 依赖注入实现。

check_delivery_configuration 的只读快照只报告 pending，不再把它当作不能读取状态的原因；不会核销或释放预留。恢复模式在一次进程中固定，人工核查后须重启。任意动态增加路由、旧客户端直接访问未导出的内部对象、运维核对正确性及存储损坏不在模型内。没有自动重试或费用清零转换。

## 证据

文档和错误合同位于 document_management_contracts.py、public_error_contracts.py，完整 HTTP 流程位于 contract_http_integration.py。会话请求查询为只读观测，新方法未改变既有会话转换绑定。模型故障与注册合同位于 model_delivery_contracts.py、model_provider_registry_contracts.py。恢复合同位于 delivery_status_contracts.py 与 physical_settlement_boundary_contracts.py。

普通门禁和附加 TLC 运行的实际结果另记入本轮决策记录，不能由本文推定通过。

## 请求恢复的观测边界

请求 ID 保持既有输入协议，路径转换器接收包括斜杠的已存在身份。请求查询按 group_id 读取原有 SessionStore 事件，不新增任务记录。model_failed 中的 COMMIT_UNKNOWN 在公开视图中投影为 reconciliation_required，并保留物理身份、操作身份和已观察用量；原 running 行、租约和预留不变。成功 model_completed 事件保留已知 usage、cache_kind 和 attempts，缓存结果不能推导为新的物理发送。

来源授权失效时完成结果继续脱敏并移除 observations；请求视图返回前再次核对 authority。公开聚合 send_status 保守保持 unknown，逐调用记录可以包含已知发送事实。原有限会话模型没有证明这些公开观测字段的完整性；新增真实接口故障回归和严格 Schema 承担这一义务，不把派生状态当作另一个执行状态机。

## 既有验收的行为迁移

保留 DeliveryContracts.test_unresolved_request_blocks_preflight_and_factory_without_invoking_application 的历史用例身份。其新断言验证预检只读报告恢复模式、账本不变；恢复工厂的认证读取及零发送由 delivery_status_contracts.live_suite 承接。旧的整进程拒绝启动已被用户要求的受限恢复行为替代，不能继续作为期望行为。

BuildRecoveryContracts.test_publication_versions_acl_and_missing_original_survive_restart 仍保留重启、撤权、过期构建拒绝与原件缺失断言；候选修订后的旧发布证据改为验证引用身份保持，且未发布的新正文不可见。没有删除权限或完整性负例。

普通封闭合同的文档场景直接调用真实原生解析函数，以免在离线 worker 中启动 HTML 解析子进程。生产 HTTP 用例单独通过真实解析子进程验证 PARTIAL、重新解析、构建拒绝及解除绑定后的恢复。两种观测边界分别报告。

## 活动输入清单

新增 combination-inputs-api-v6.json，保留 v1 至 v5 的全部原始字节。v6 继承 v5 的运行定义、模型输入、共享入口和合同身份，只增加 BatchRecovery 的一个正例、两个负控、四个模型输入文件及本轮 API 合同身份。每个新增运行定义和输入文件均绑定 SHA-256。组合合同验证没有移除或替换历史义务。

发送前校验的字段丢失负控原先依赖 ERROR_FIELDS 的末尾排版。增加响应事实字段后，旧文本替换没有删除任何字段。负控现在通过语法树移除 known_zero_send，仍验证 error_details 函数摘要不变而模块级绑定报告漂移。没有移除断言，也没有更新该变体的期望为通过。
