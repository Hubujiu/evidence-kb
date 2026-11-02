# 不加载知识库的基础层消费者

这个例子装配三个插件：`foundation.llm`、`foundation.storage` 和 `example.notes`。消费者通过公开 `LlmClient` 与 `StorageClient` 调用服务，将模型回复写入对象存储，并用持久 KV 保存上一条回复。它没有依赖知识库、检索、资料权限或会话工作流。

`example.notes` 提供 `application.run/v1`，作为应用入口。基础层服务本身不冒充应用入口；`build_foundation_application` 使用相同 Rust 合同编译和生命周期管理来装配它们。

## 直接运行

从仓库根目录执行：

```sh
uv sync --locked --group dev
cargo fetch --locked
cargo build --locked --offline -p agent-kernel
uv run --no-sync python examples/foundation-consumer/main.py --root .local/foundation-notes --message "first message"
uv run --no-sync python examples/foundation-consumer/main.py --root .local/foundation-notes --message "second message"
```

默认使用示例中的 `LocalTextProvider`，它实现公开 ModelAdapter 接口，只把输入作为确定性文本返回，不访问网络。返回的 `usage` 是 `null`，因为它不提供模型 token 观测。第二次运行的 `previous_reply` 应等于第一次的回复；dataset 身份保持一致。示例会实际读回对象并验证 hash，再读回 KV 值验证持久写入。

如果本机没有自动定位到本次构建的原生库，将 `KB_AGENT_KERNEL_LIBRARY` 设为该库的绝对路径。Linux 构建产物通常为 `target/debug/libagent_kernel.so`，其他平台使用对应动态库文件。

## 使用配置中的模型

准备符合 `config/models.example.json` 格式的模型文件和它声明的凭据环境变量，然后运行：

```sh
uv run --no-sync python examples/foundation-consumer/main.py --root .local/foundation-notes --models-config .local/models.json --route generation --message "Explain the difference between a service and its provider."
```

这条命令会向所选 Provider 发出一次模型请求。消费者插件代码保持不变；配置决定 Provider、模型、适配协议及预算。物理请求账本使用 `provider.primary` 的结构化存储，审计记录保存在所选 root 的 `model-audit` 目录。正常、失败与 UNKNOWN 的请求继续使用项目既有账本规则。

## 使用其他存储配置

`--storage-config` 接受已初始化、包含 dataset_id 的存储配置。消费者明确声明以下能力：

| 绑定 | 合同 | 用途 |
| --- | --- | --- |
| `notes.state` | `storage.kv.durable/1` | 保存上一条模型回复 |
| `notes.objects` | `storage.object/1` | 保存并校验回复原文 |
| `provider.primary` | `storage.relational.guarded/1` | 配置模型 Provider 的物理请求账本 |

对象存储可以更换为 S3，持久 KV 可以更换为满足该合同的其他 Provider。一个只有过期缓存语义的 KV 实现不能自动满足持久账本或持久 KV 的要求。结构化预算 schema 由消费者显式注册，通用存储层不导入知识库 schema。

## 验收范围

统一集成验收会在干净子进程中运行这个例子，并在完成模型、KV 和对象读写之后检查没有加载任何 `kb_platform.knowledge*` 模块。另一组用例在相同消费者中更换 OpenAI-compatible 与 Anthropic 配置，使用实际 HTTP 客户端连接本地协议夹具。这验证独立消费和配置替换，不是远端模型质量或性能评测。完整验收说明见 [基础层与知识库组合验收](../../docs/foundation-acceptance.md)。
