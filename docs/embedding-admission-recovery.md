# Embedding 预算拒绝与续建

物理请求预算耗尽时，创建或续建索引会返回 `EMBEDDING_BUDGET_INSUFFICIENT`（HTTP 503）。该批次保持 `RETRY`，不消耗重试次数，不增加熔断失败数；已有完整向量保持可复用。补充明确授权的预算、确认没有未决物理请求后，调用已有的 `POST /knowledge-bases/{kb_id}/builds/{build_id}/resume`。

如果共享物理账本存在未决请求，则返回 `EMBEDDING_LEDGER_BLOCKED`，同样保持可恢复状态。必须先核对原物理请求，不能仅增加额度或改用新账本绕过未决记录。恢复时仍验证冻结来源、权限、模型和实现依赖；依赖变化不自动重绑定旧向量。

## 为什么能判定为零发送

`MeteredTransport` 在调用底层 HTTP transport 之前检查账本并预留预算。只有这个边界确认的预算不足或账本未决，才抛出带原因的 `CapturePreflightError`。Embedding 调度器根据异常类型和允许的原因映射到既有 `EmbeddingPreflightError`；Google Gemini API 与 Vertex API 都保留该异常。

普通异常、相同错误文字、账本读写故障和没有零发送证明的传输失败不获得此分类。真实未知结果继续保持 UNKNOWN，不自动重发。恢复为每次新逻辑尝试分配新身份，已完成批次不再请求供应商，历史错误、费用与未知预留不清零。

## 已有 UNKNOWN 如何处理

升级不会按 `CaptureAdmissionError` 字符串批量改写旧 UNKNOWN。旧记录没有新的类型化零发送证明，不能仅凭预算当前已耗尽或没有找到响应文件推断其未发送。现有成功结果恢复路径仍要求匹配的采集记录、输入、模型与已结算账本；无法证明的旧记录保留为需审计项。

2026-10-30 的首次真实验收保留了 32 个已完成物理请求及一个错误分类为 UNKNOWN 的逻辑任务。该原始现场没有被重置或续发；修复验证使用新的独立身份，并累计保留旧费用。

## 验证

统一离线入口自动发现 `embedding_jobs_contracts.py` 中的新用例。真实 Google 客户端、物理计量 transport 与 SQLite 账本配合合成 HTTP 响应，覆盖预算耗尽和未决拒绝、零发送、原账本不变，以及错误文字不能充当证明。新用例在旧分类器上检出四个失败。

该模块已有的 `live_contract_suite(dsn)` 增加真实 PostgreSQL 续建用例：首批完成、后批预算拒绝、重复准入不消耗次数、补充关联预算、新任务管理实例恢复、已完成批次不重发，最终检查累计账本和熔断状态。该套件共 11 项通过；使用合成 HTTP，不代表供应商答案质量。

运行真实数据库合同需要已初始化项目表结构的专用测试数据库。通过应用角色连接，以随机测试租户隔离；使用现有统一合同，不运行额外模型调用：

```powershell
uv run --no-sync python -c "import os, unittest; from kb_platform.rag_testing.embedding_jobs_contracts import live_contract_suite; result = unittest.TextTestRunner(verbosity=2).run(live_contract_suite(os.environ['KB_DSN'])); raise SystemExit(not result.wasSuccessful())"
```

完整项目门禁仍为 `uv run --no-sync python scripts/check_project.py`。真实供应商、全产品 HTTP 与答案质量的结果另行记录，不能用上述组件合同代替。

## 同轮 HTTP 验收发现的阻塞

PostgreSQL 中尚未发布可选语义 Page 索引时，`current_manifest` 现在抛出 `FileNotFoundError`，与文件存储表达的缺失状态一致。知识问答可继续走已发布的原文索引。此前普通 `ValueError` 被当成索引损坏，导致新知识库问答在模型调用前返回 HTTP 409。合同同时验证损坏校验和、过期、权限拒绝和模型不匹配仍会拒绝，不能借此吞掉真实错误。

模型输出约束增加明确的 Schema 边界，并要求返回任务数据实例。实际 CSV 联表冒烟曾收到完整 Schema 定义而非工具决策；修正后同一冻结题通过，金额为 120 元。原严格 Schema 校验、一次格式修复上限与费用记录不变，没有把 Schema 回显转换成合法答案。回显拒绝合同保持覆盖；提示改善的证据来自实际调用，不能由字符串断言证明模型可靠性。

## 2026-10-30 验收记录

统一门禁 `192da1659ded4671aec7431b39ec56e9` 全部通过：346 项 Python 合同、46 项 Rust 测试、24 项评分器控制；Rust 格式、Clippy、构建、源码边界与 lint 均通过。真实 PostgreSQL 续建合同另有 11 项通过。评分器控制不是产品成绩。

真实 HTTP 验收使用 `reports/e2e-preflight-fix-20261030-01/` 的冻结计划与统一 runner/scorer。60 份合成原件完成上传和解析，两个索引发布，索引阶段新增 86 次 Embedding。问答阶段复用这些已发布索引；索引阶段、可选索引缺失修复后、输出提示修正后的源码哈希分别保存，不能称为最终源码从零冷启动的全链路运行。

提示修正后的固定单轮分母为 24：`smoke-03` 为 3 PASS，`remaining-01` 为 17 PASS、4 ERROR，没有删去失败题。20 PASS 表示统一确定性证据与答案行为合同通过，不能等同于独立语义裁判通过或生产业务金标质量。

| 未通过用例 | 观测结果 |
| --- | --- |
| `dev-md` | 重排重复候选 ID，`INVALID_RERANK_OUTPUT`，工具失败 |
| `dev-workflow` | 规划引用未提供的证据，`UNKNOWN_EVIDENCE_REFERENCE` |
| `dev-enumeration` | 一次格式修复后仍为 `AGENT_SCHEMA_MISMATCH` |
| `holdout-workflow` | 一次格式修复后仍为 `INVALID_JSON_SYNTAX` |

上述失败属于本次仍未解决的模型输出可靠性问题，保留错误响应、最终请求和费用；没有放松引用、候选集合或 JSON 校验，也没有持续重试直到通过。此前 `smoke-01` 的 3 ERROR、`smoke-02` 的 2 PASS / 1 ERROR 同样只读保留，不计入修正后 24 题的成功分母。本次修复可供代码审查，但不能据此宣称全链路或生产准入已通过。

两组多轮会话 `conversations-01` 均通过统一合同，共 7 个实际用户轮次，并核验两次最终 HTTP 幂等回放。人工查看最终回答时，预算更正正确保留为用户提供的 520 元，最终来源切换回答引用值班资料。来源切换中间轮次却错误声称没有切换授权，仍需语义复核；没有将最终证据合同通过扩大为所有会话措辞正确。未新增模型裁判，未验证 128K 饱和长会话。

原文审计确认 60 个上传回执的哈希匹配冻结原件，两个已发布索引状态正确；7 项只读 HTTP 运维检查通过且未增加物理请求。共核验本轮 280 条实际请求与账本身份、payload 哈希：166 次模型调用，114 次 Embedding（含 86 次索引构建），全部 COMPLETE，未决和 usage 未知均为零，没有超预算。包含失败输出和早期冒烟的 usage 为模型输入 1,394,925、输出 36,315 token，Embedding 输入 3,241 token；这些是供应商 usage，不是启发式估算，也不是货币费用。

累计账本为 1,153 个物理请求，前序快照未改变，未决为零；历史 3 条 usage 未知记录仍保留。未测量货币费用，不能把未知 usage 记作免费。全量原始产物保存在本机上述报告目录及对应 `.local/` 私有运行目录，不将供应商配置或身份文件提交仓库。
