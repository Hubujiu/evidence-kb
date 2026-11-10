# LLM 失败、未知用量与预算状态

## 采用的参考与适用范围

本次修复对齐 DeepSeek Harness 的三个既有职责：适配器归一化失败事实、Agent 持久化失败尝试、独立策略消费这些事实。参考固定于上游提交 [`ce7073d51254c26cd63b22a60835b8f948deb447`](https://github.com/deepseek-ai/deepseek-harness/commit/ce7073d51254c26cd63b22a60835b8f948deb447)，避免把持续变化的主分支当作设计依据。

| 参考实现 | 本项目采用的规则 |
| --- | --- |
| [`adapter-failure.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/llm/llm/src/adapter-failure.ts) 的 `normalizeLlmFailure`，以及 [`types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/llm/llm/src/types.ts) 的 `LlmFailure` | 提供者边界产出可序列化失败事实；公共桥复用同一归一化入口，重试由策略决定。 |
| [`agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/core/agent-loop/src/agent.ts) 的错误与中断处理 | 失败尝试先保留为事件；错误不能吞掉已观察用量，也不能把失败输出作为完成结果执行。 |
| [`turn-usage.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/llm/token-meter/src/turn-usage.ts) 的 `deriveTurnTokenUsage` | 生命周期或用量证据不完整时，精确统计不可用；缺失不补零。 |
| [`llm-retry/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/llm/llm-retry/src/index.ts) | 重试是独立策略；错误映射、传输解码和状态查询不偷偷重试模型。 |

DSH 的错误码 `UNKNOWN` 表示异常尚未被归入其稳定错误分类，不能据此推出是否发送、是否收费或是否应重试。本项目已有的物理预算预留、存储提交不确定性和应用预算属于自己的业务约束；它们继续使用原账本。这里复用上述边界原则，没有声称 DSH 提供了本项目的持久预算算法，也没有新增一套 UNKNOWN 状态机或复制其默认重试次数。

Rust 注册表、租约和统一 LLM 类型保持原有所有权。这次缺陷位于 Python Provider executor、公共 JSON 适配、应用统计与交付状态读取，因此修复在这些边界落地。

## 根因

之前把“公共 RPC 已进入调用”映射成 `model_sent=True`。预算存储异常发生在 HTTP 发送前，如果没有被有限的预算错误分支识别，就可能被当成发送后的未知失败。Luna 捕获层还存在只识别若干异常类型的遗漏，SDK 解包可能丢掉原始原因。结果是实际 HTTP 为零，应用却出现一次未知用量。

同时，状态接口在账本读取失败后访问 `ledger.ledger.summary()`，把所有账本假定为批次账本。普通账本没有该属性，原本应该返回的“账本不可用”又变成了 HTTP 500。

另外，HTTP 解码原先一起处理内容和 usage；内容先出错时，合法 usage 还没被提取就丢失了。

## 统一处理规则

`model_errors.py` 是现有失败 DTO 的唯一归一化入口。`llm.service` 和知识库模型公共合同共享它，保留稳定错误码、HTTP 状态、重试提示、发送事实、物理身份、原始 usage、trace 与应用调用记录。

1. **在实际边界标记事实。** 请求验证、构造、授权、准入和预算预留发生在发送前；这里失败明确携带 `model_sent=False`。存储异常保留原类型、code 和 cause，不靠扩大错误码名单决定是否发送。
2. **RPC 信息只描述 RPC。** handler 尚未进入时可以证明未发送；handler 开始后只说明调用被分派，不能推断模型已发送。`wire_read` 是已有结果的下载，下载失败不能归类为调用前拒绝。
3. **usage 独立于内容成功。** HTTP 响应的用量先提取，随后校验模型内容；无效文本、结构或后置校验失败仍携带已观察 usage。缺失或无效 usage 保持不可用。
4. **保留策略边界。** `model_sent=False` 的失败不新增物理尝试，也不会由 SDK 自动重试。发送后未知仍沿用原保留预留、阻断未决请求的策略。RPC 结果丢失而没有提供者事实时，应用继续保守保留未知用量，不能伪造已发送或零消耗。
5. **事实进入持久事件。** `model_failed` 沿用相同的发送事实、物理身份、拒绝原因、attempts 与 usage。应用逻辑请求 ID 与物理请求 ID 分开。

`code` 和 `preflight_code` 含义不同。例如 Luna 原外部错误码仍为 `RAG_PHYSICAL_REQUEST_ADMISSION_FAILED`，预算原因在 `preflight_code=BUDGET_INSUFFICIENT`；公共桥不会为了表示拒绝而覆盖原 code。

## 不同“未知”的处理

| 场景 | 发送与用量事实 | 处理 |
| --- | --- | --- |
| 预算不足或发送前授权拒绝 | 明确未发送 | 新增 HTTP、应用 attempts 与未知用量均为零。 |
| 预算数据库拒绝写入，已知未提交 | 明确未发送 | 保留存储错误，应用不记一次模型调用。 |
| 预算预留提交返回 `COMMIT_UNKNOWN` | 模型明确未发送，预算写入结果不确定 | 应用不增加未知模型费用；已经存在或可能存在的物理预留继续保留，不能删掉或盲目重试。 |
| 模型请求已发送，读取响应中断 | 已发送，usage 不可用 | 物理账本保留 UNKNOWN 与预留，后续准入阻断；重启不会绕过。 |
| 响应内容无效，但 usage 有效 | 已发送，usage 已知 | 返回内容错误，按已观察用量结算，保留物理 ID，不新增未知用量。 |
| 公共 RPC 结果丢失 | 客户端无法证明模型是否发送或取得 usage | 客户端保守记账；提供者物理账本仍是自身状态的权威，公共桥不重发。 |
| 状态查询无法读取账本 | 用量、余额及未决数不可核验 | HTTP 状态查询成功返回 `BLOCKED` 与读取原因，未知字段为 `null`。 |

当前 `attempts` 在 RPC 结果不可观察时包含保守计入的潜在尝试，不能拿它单独证明物理 HTTP 数；实际发送应核对提供者账本与物理身份。

## 只读状态接口

普通账本与批次账本公开同一个 `status_snapshot()`。交付层只读取该快照与公开 caps，不识别具体账本内部布局。读取失败返回 `BUDGET_READ_UNAVAILABLE`、`usage_available=false`，用量、余额和未决数为 `null`；它们不再看起来像零。字段与历史审阅规则见 [交付预算状态查询](budget-status.md)。

若当前批次可读、前序账本不可读，仍展示当前可信数据，累计数据与累计未决数保持未知，准入状态为 `BLOCKED`。控制台明确展示不可核验信息。状态读取不发送模型请求，不创建预算记录，也不修补已有账本。

## 回归与交付验收

所有新增回归进入原统一检查入口 `uv run --no-sync python scripts/check_project.py`：

- `model_http_outcome_contracts.py`：发送前存储异常、COMMIT_UNKNOWN、内容失败保留 usage、真实发送后未知。
- `model_failure_boundary_contracts.py`：Luna SDK 经公共 LLM JSON 合同传递预算拒绝与存储提交不确定性。
- `llm_contract_contracts.py`：端点在 handler 前关闭、RPC 结果丢失、下载失败、已观察响应发布失败与请求验证。
- `delivery_status_contracts.py`：普通及批次只读状态、父账本不可用、真实 ASGI 状态路由；HTTP 路由检查加入 foundation integration。
- `foundation_knowledge_integration.py`：实际 SQLite 故障与 loopback HTTP，穿过存储、LLM、知识库和持久事件；原发送后断线与重启不重放场景继续验证。

这些是确定性合同和受控本地集成，覆盖失败语义与费用观测；不替代外部模型质量、生产数据库故障概率或吞吐验收。执行不新增付费模型、Embedding 或裁判请求。
