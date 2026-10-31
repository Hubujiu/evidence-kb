# 成熟框架替换计划与当前边界

更新：2026-10-30。目标是移交通用能力的维护责任，保留项目特有的权限、证据、预算和请求恢复合同。没有证据支持的能力不通过改名、默认假设或另建评测体系宣称完成。

## 逐项状态

| 原计划项 | 状态与交付 | 保留的业务边界 |
| --- | --- | --- |
| 手写 Schema 引擎 | PR #5 采用 jsonschema；本轮补齐 Pydantic 类型生成 Decision/Answer/Verdict 和工具扩展字段 | 严格类型、重复键、非有限数值、关键字白名单及业务校验 |
| 单提供者 SDK | PR #6 采用 OpenAI SDK 的 Luna 适配 | SDK 隐式重试关闭；物理发送、预算、错误分类和 UNKNOWN 仍由项目管理 |
| Agent 编排 | PR #7 曾采用 LangGraph；当前迁移为 Rust 生命周期内核与插件化 loop，移除 LangGraph/LangSmith 依赖 | 权限、证据、步数/时间/费用门禁、单写者及恢复权威保留 |
| 服务插件化 | Rust 宿主管理依赖、作用域和租约；profile 选择模型、上下文、会话、工具和 loop，RAG 作为 kb_query 接入 | 知识库 KnowledgeSessions 与物理请求账本保留唯一权威；通用临时会话不承担业务恢复 |
| 类型化工具集中注册 | 本轮完成工具能力开关、参数提取、规划动作与重试资格注册 | 原文授权、引用存在性、表格操作、流程用户事实等业务处理仍在原节点 |
| Markdown 解析 | PR #8 采用 markdown-it-py | 原文切片、坐标、哈希、证据锚点和历史表格导航 |
| 专用 reranker 配对试验 | PR #9 采用 CrossEncoder，完成固定候选对照；生产切换未获准入证据 | 证据覆盖判断独立于排序分数；不切换生产默认 |
| 重试所有权与通用调度 | 本轮只读工具调度采用 Tenacity；运输层仍唯一负责物理请求重试 | 非只读、UNKNOWN、权限/预算拒绝不重试；Retry-After、累计等待及截止时间保持有界 |
| 上下文 token 估算 | **采用现有启发式估算；精确计量研究终止，不再作为验收阻塞** | 完整消息、Schema、输出预留与安全余量继续计入；估算不替代供应商实际 usage |

## 本轮移除与兼容性

`agent_protocol.py` 使用已依赖的 Pydantic `TypeAdapter` 从严格 TypedDict 生成协议 Schema，嵌套结构通过 `WithJsonSchema` 使用生成的内联子 Schema，不开启 `$ref` 或外部解析。删除 `agent_service.py` 中手写的 Decision、Answer、Verdict 字段树和工具扩展字段树，测试导入迁移到新模块，不保留旧定义副本。

本轮核对旧三份协议与新 Schema：去除 `title` 注释后结构相同。运行时仍通过既有严格 jsonschema 边界校验，没有使用模型默认值补齐缺失字段，也没有放宽字符串、整数或布尔类型。动态候选引用枚举与长度依然使用小型 `object_schema` 构造器，它没有承担通用校验算法。

`agent_tool_registry.py` 统一工具参数投影和重试资格，删除原节点中的重复参数分支、动作集合及扩展 Schema 拼接。生成式工具协议仍为 JSON 决策对象；不启用未经验证的原生 tool_calls，也不叠加 Pydantic AI 编排。

生成 Schema 中新增的标题注释会改变规划请求文本；现有缓存键包含 Schema 和请求载荷，因此不会复用不同请求的回答。类型化工具阶段版本为 `agentic-rag/5.16.0-typed-tools`，后续原生运行时改造以当前 `knowledge_service.py` 的版本标识为准。上下文计量仍在最终 Schema 指令插入后校验输入限额；不宣称线上回答分数不变。

原 `agent_service.py` 已按会话服务、单轮状态、规划、模型与答案核验职责拆分。默认入口由 `knowledge_profile.py` 装配，原生内核不导入业务实现。实际接口、默认插件和构建方式见 [插件宿主](plugin-harness.md) 与 [Rust 运行时](rust-agent-runtime.md)。

`ToolExecutor` 的重试循环改用 Tenacity `Retrying`、`retry_if_exception`、`wait_exponential` 和 `stop_after_attempt`。项目只保留允许重试的分类及停止政策。Retry-After 不为凑预算而截短；空结果是成功，截止后返回的结果不得交付，取消和睡眠中断保留 UNKNOWN 回执。工具结果的复用仍需调用方重新授权。

## 重试所有权

| 层 | 所有权 |
| --- | --- |
| Rust loop / 生命周期内核 | 每个效果显式关联结果，不自动重放模型、工具或业务节点 |
| OpenAI SDK | max_retries=0，不进行隐藏发送 |
| `_Transport` | 唯一物理请求重试；每次新物理身份关联上次请求，先准入再发送 |
| Agent 格式修复 | 有界的新逻辑尝试，沿用物理账本和总预算 |
| ToolExecutor / Tenacity | 仅显式只读工具的已确认瞬时故障；search_pipeline、search_semantic_pages 禁止外层自动重试 |
| Google 限流/认证 | 保留提供者专用协议，不把认证或限流状态机当作通用工具重试 |

运输层包含发送回执、重试血缘、预算和未知结果转换，未套用通用装饰器来隐藏这些行为。没有新增全局重试器或另一份费用账本。

## 上下文采用启发式 token 估算

按 2026-10-30 的产品决定，不再研究跨模型精确 token 计量，也不把 tokenizer 绑定列为后续验收前置条件。同一提供商的不同模型可能使用不同分词与消息封装；这里选择不依赖模型专用 tokenizer 的本地估算，而不是声称所有模型都无法精确计量。

参考成熟项目的公开实现（2026-10-30 查阅）：

- [LangChain count_tokens_approximately](https://reference.langchain.com/python/langchain-core/messages/utils/count_tokens_approximately) 默认使用每 token 4 个字符和每消息 3 个额外 token，计入角色、工具调用与工具定义；文档明确这是近似值，4 字符比例针对常见英文文本。
- [Pi 上下文压缩实现](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/src/core/compaction/compaction.ts) 使用字符数除以 4 的启发式，计入工具参数；可用时用最近一次 usage 加后续消息估算，历史编辑或压缩使旧 usage 失效时重新估算，并预留上下文空间。

本项目保留 `context_budget.estimate_messages` 的现有公式：完整消息规范化 JSON 的 UTF-8 字节数 + 每条消息 16 + 固定 32。中文、代码、转义字符及结构化字段均计入，Schema 在最终校验之前插入。该公式比常见字符数除以 4 更保守，可能提前裁剪；它是工程预算单位，不保证任意模型的真实 token 上界。本次不改变公式、预算或裁剪结果，也不引入新的依赖或模型调用。

输入额度仍取配置额度与“模型窗口减输出预留和安全余量”的较小值。默认 `strict_tokens=false`；`exact=false`、`tokenizer_verified=false` 保持事实含义。显式要求 `strict_tokens=true` 的旧调用仍在发送和预留前拒绝，避免将估算伪装成精确承诺；该保护不是待实现的精确计量功能。

供应商实际 usage 与发送前估算分别记录，缺失 usage 仍为 unknown，费用和 UNKNOWN 预留不能被估算覆盖。历史经过裁剪、权限过滤或 Schema 变化后，当前路径重新估算最终消息，不套用旧 usage 来降低准入预算。BGE 的模型专用 tokenizer 继续服务于其组件输入长度限制，与对话上下文估算职责不同。

## 验证与停止边界

新增 `framework_completion_contracts.py` 接入统一发现，覆盖类型/可选字段/额外字段、工具功能开关与默认参数、重试次数、Retry-After、累计等待、截止/迟到结果、UNKNOWN、回执复用以及未验证 token 能力的拒绝行为。既有权限、证据、预算、恢复和 SDK 合同继续运行，没有删除测试或调整门槛。

本轮不新增模型、Embedding 或语义裁判调用。统一评分器复用冻结控制 trace，不能将其 PASS 当作生产质量验收。生产 reranker 保持不切换的实验结论；上下文采用启发式估算，精确 token 计量不再属于计划范围。真实供应商、数据库和答案质量验收仍按各自证据判定，不因该范围决定自动通过。
