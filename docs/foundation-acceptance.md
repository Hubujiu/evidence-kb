# 基础层与知识库组合验收

交付边界分成两个可以分别运行的对象：通用插件基础层，以及建立在它之上的知识库应用。基础层提供模型与存储合同、注册身份和生命周期；知识库负责资料、权限、证据、会话和工作流。替换 Provider 应当修改配置与装配，不能要求消费者导入具体模型客户端或数据库实现。

DSH 对齐范围、固定上游提交和 Rust crate 依赖方向见 [DSH Rust 基础层](dsh-rust-foundation.md)。本文说明如何验收实际应用是否消费这些基础能力，不把合同类型、示例输出或 Provider 名称当作端到端成功的证据。

## 默认装配的责任

| 插件 | 责任与依赖 |
| --- | --- |
| `foundation.storage` | 提供 `storage.service`，按合同与 dataset 身份绑定存储；不决定知识库表结构或 ACL |
| `foundation.llm` | 提供 `llm.service`，注册模型 Provider、转换 wire、保留物理请求和用量事实；可消费存储与公开 policy 合同 |
| `knowledge.authority` | 消费存储，提供知识库当前权限、发布版本与原文身份的权威视图 |
| `knowledge.access` | 认证、租户与资源访问规则、模型处理许可 |
| `knowledge.model_policy` | 以公开 JSON policy 连接发送前授权和领域输出校验，避免在跨语言调用中丢失 Python 回调的约束 |
| `knowledge.model` | 消费 LLM 服务，应用结构化输出合同、上下文策略、单轮预算和校验后缓存确认 |
| `knowledge.sessions` | 会话 revision、持久请求身份、幂等结果与历史 |
| `knowledge.data` | 文档导入、原文、索引、检索与证据；消费模型和存储能力 |
| `knowledge.context` | 从当前授权证据和会话历史构建实际模型上下文 |
| `knowledge.workflow` | 编排检索、生成和验证；不创建另一套 Provider 注册或物理预算账本 |
| `knowledge.entry` | 对外应用入口，将公开请求送到声明的合同 |

实例名由配置决定，表中的名称是实现标识。验收不以固定节点数量为成功条件。增加或删除内部节点不能改变租户、注册代次、事务或请求费用的语义。

## 脱离知识库的消费者

[foundation-consumer 示例](../examples/foundation-consumer/README.md) 使用 `build_foundation_application` 装配 `foundation.llm`、`foundation.storage` 和自己的 `example.notes` 应用插件。应用只依赖公开合同，实际调用模型、写入并读回对象、写入并读回持久 KV。结构化账本 schema 由消费者声明，没有通过知识库工厂隐式安装。

示例默认使用公开 ModelAdapter 接口实现的本地文本 Provider，不产生外部请求；传入 `--models-config` 后由配置选择 HTTP Provider，消费者代码不变。集成验收在干净子进程中分别运行这两条路径，确认完成调用后仍没有加载 `kb_platform.knowledge*`。再次启动同一存储配置会读回上一条回复并保留 dataset 身份。

应用入口继续满足 `application.run/v1`。LLM 或存储服务出口不能直接充当应用入口，这个例子通过真正的消费方插件实现该合同，没有放宽 Rust 装配校验。

## 同一知识库消费者更换 Provider

`kb_platform/rag_testing/foundation_knowledge_integration.py` 使用生产装配入口和默认 profile。两组配置运行相同知识库业务插件和操作：

| 组合 | 模型请求路径 | 原文存储路径 |
| --- | --- | --- |
| OpenAI-compatible + files | 实际 HTTP 客户端 → `127.0.0.1` 的 `/v1/chat/completions` | 生产文件 Provider |
| Anthropic + S3 | 实际 HTTP 客户端 → `127.0.0.1` 的 `/v1/messages` | 生产 S3 Provider / boto3 → 本地 S3 协议夹具 |

模型服务的配置文件和存储服务的配置文件不同，知识库业务插件配置保持一致。两组都使用 SQLite 保存领域与会话记录、使用真实 BM25 索引。这个矩阵证明模型 wire 和对象存储 Provider 的替换；它不声称在此用例中完成了 PostgreSQL、MySQL、Redis 或所有向量数据库的替换验收。其他后端继续由统一存储矩阵覆盖。

一次完整的业务验收执行以下步骤：

1. 通过配置的身份文件认证，创建知识库，导入并发布原文。
2. 实际建立 BM25 索引，检索引用，再经当前对象存储读回原文并验证内容。
3. 建立会话，执行计划、检索决策、回答与验证四次模型请求。
4. 核对真实发送的最终证据上下文、回答和引用，核对物理请求与预算数量。
5. 用相同 request_id 和 revision 重放请求，确认结果一致且没有新增 HTTP 请求。
6. 检查跨用户会话与跨租户知识库访问被拒绝；撤销原文模型处理许可后，旧回答不能绕过授权直接从幂等结果返回，历史敏感内容必须被隐藏。

模型回复由确定性夹具生成，其中引用回答读取的是实际发送的证据。测试不使用真实付费模型、Embedding 或 judge，不对生产答案质量、token 节约或在线延迟作出推断。

## 已发送但结果未知

另一场景让本地模型端点在完整接收 HTTP 请求后断开连接。验收必须同时确认：

- 应用返回 `PROVIDER_OUTCOME_UNKNOWN`，不发布答案或可执行结果。
- 已发送次数和未知用量不消失；物理账本保留 UNKNOWN 以及原预留。
- 审计记录保存原 physical_request_id，不能用新的逻辑请求 ID 掩盖同一个未决发送。
- 关闭并重新装配应用后，同一幂等请求返回已有结果，不重新发送。
- 新请求在旧物理请求未决时被准入拒绝，原账本摘要和物理审计记录保持不变。

服务换代、应用重启和兼容层转换都不能把未知用量改为零，不能把“没有收到结果”解释为“没有发生发送”。测试通过真实套接字断连产生 UNKNOWN，不 mock 原生调用或请求账本。

## 预留期间撤权

`foundation_transport_integration.py` 通过公开 LLM JSON 合同调用真实 HTTP embedding 适配器。一个使用现有持久预算实现的夹具在预留完成时撤销许可。发送器必须在这一时间点之后重新调用公开 policy，才能开始 HTTP 发送。

验收分别检查正常授权的真实请求、原先已撤权的准入拒绝、预留后撤权的明确未发送结果。最后一种保留原 physical_request_id 和 ERROR 记录、释放未使用的预留，且服务端收到零条请求。它与已经发送后断连的 UNKNOWN 有不同的事实依据。

## 执行与报告

正式验收继续使用仓库统一入口：

```sh
uv run --no-sync python scripts/check_project.py
```

调试单个应用验收模块时，先构建本次原生库，并将 `KB_AGENT_KERNEL_LIBRARY` 设为该库的绝对路径：

```sh
cargo build --locked --offline -p agent-kernel
uv run --no-sync python -m unittest kb_platform.rag_testing.foundation_knowledge_integration kb_platform.rag_testing.foundation_transport_integration kb_platform.rag_testing.foundation_consumer_integration
```

这组测试作为统一 checker 的受控集成步骤执行，和禁止网络的离线合同 worker 分开。它需要允许本机回环地址上的临时监听端口；端口由操作系统分配，不连接外部模型服务。不得因为沙箱禁止本地套接字就跳过测试或把测试改成只验证静态配置。

报告应分别列出基础层独立消费、知识库应用组合、实际后端矩阵和离线协议合同的结果。未执行的部分保持未执行状态；本地协议夹具成功不等于已验证远端供应商的全部能力。
