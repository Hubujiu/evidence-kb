# 成熟框架替换计划与当前边界

更新：2026-10-29。目标是移交通用能力的维护责任，保留项目特有的权限、证据、预算和请求恢复合同。没有证据支持的能力不通过改名、默认假设或另建评测体系宣称完成。

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
| 匹配模型的 token 计量 | **未完成：缺少 Luna 模型与 tokenizer/chat framing 或计数接口的可验证绑定** | 保留标注为估算的计量；strict_tokens 继续在发送和预算预留前拒绝 |

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

## token 计量为何仍受阻塞

项目公开配置使用 `codex/gpt-6-luna`，现有 AgentModel 能力声明为 tokenizer_verified=false，且目前走兼容网关的 Chat Completions。检查的 [tiktoken 模型映射](https://github.com/openai/tiktoken/blob/main/tiktoken/model.py) 没有该模型绑定；不能去掉前缀或按名称猜测编码。即便选定文本 tokenizer，也不能据此确认网关的消息封装和工具/Schema 开销；[官方计数示例](https://developers.openai.com/cookbook/examples/how_to_count_tokens_with_tiktoken) 也区分模型相关的消息计量。

本轮没有添加一个未经准入的 tokenizer 后备路径来降低工程估算值。精确替换仍需要提供者确认的模型 revision、tokenizer/chat template 或计数接口合同，并针对实际最终载荷核验。该依赖未满足时，不能将整体计划标为全部完成。已有 BGE 组件的真实 tokenizer 长度检查不等同 Luna 对话请求的计量能力。

## 验证与停止边界

新增 `framework_completion_contracts.py` 接入统一发现，覆盖类型/可选字段/额外字段、工具功能开关与默认参数、重试次数、Retry-After、累计等待、截止/迟到结果、UNKNOWN、回执复用以及未验证 token 能力的拒绝行为。既有权限、证据、预算、恢复和 SDK 合同继续运行，没有删除测试或调整门槛。

本轮不新增模型、Embedding 或语义裁判调用。统一评分器复用冻结控制 trace，不能将其 PASS 当作生产质量验收。生产 reranker 切换和精确 token 计量均不伪称已实现；前者已有不切换的实验结论，后者仍是明确的外部能力阻塞。
