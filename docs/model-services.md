# 公共模型服务与提供商插件

`model_service.ModelService` 是 Rust 注册表的 Python 执行桥，路由、代次与调用状态唯一位于 `llm-service::Registry<T>`。知识库、通用 Agent 和辅助插件依赖
`models` 或结构化文本服务 `llm`；供应商 SDK、HTTP 格式和认证停留在适配器中。内部协议
为 `model-service/1`，采用同步请求/响应。模型能力是显式声明，不因接口兼容而推定支持。

Rust 公共层位于 `rust/llm-service`，参考 DSH 固定提交 [ce7073d 的 LLM 协议](https://github.com/deepseek-ai/deepseek-harness/tree/ce7073d51254c26cd63b22a60835b8f948deb447/packages/llm/llm)。它提供独立异步 Adapter、注册表、准备调用、七类流事件与响应组装，不依赖生命周期内核或知识库。Python 的 CHAT 调用和结构化 completion port 通过 `llm_protocol` 使用同一 Rust prepare/admit/complete/release 算法、请求检查与结果规范化，保留原始 usage，并增加规范化 usage/finish 信息。SDK 的原生异步能力与内置同步 Provider 分别验收；固定语义、上游差异和参考合同见 [Rust 服务基础](dsh-rust-foundation.md)。

## 默认知识库与公共服务

默认 `foundation.llm` 发布 `llm.service/1`，`knowledge.model` 以 `LlmClient` 消费，提供者配置与资源装配不再放在知识库模型工厂里。`plugins.llm.config` 支持 `config_path` / `providers_config`，`plugins.model.config` 只接受 routes、policy、budget。模型层的公共操作与请求 schema 随 `config/contracts/knowledge-v1.json` 生成。

`LlmEndpoint`/`LlmClient` 负责跨插件的数据投影；媒体与大消息复用 `contract_transfer`，不传 Python 对象或回调。可选 `llm.policy` 服务将发送许可与领域校验带回声明的策略插件。所有操作绑定相同注册身份；旧端点不能在热替换后继续向新提供者发送。

结构化输出先产生 completion 和验证回执，消费方执行引用/业务校验后调用 settle。应用缓存只有确认后才写入。错误穿过 JSON 仍保留 model_sent、physical_request_id、preflight_code、trace 和 call_record，UNKNOWN 不变成未发送或零消耗。

独立构造入口为 `foundation_services.build_foundation_application`；底座不要求知识库 schema、权限或 workflow。完整依赖方向和验收见 [底座与应用架构](foundation-architecture.md)。

## 实例与生命周期

低层通用 Agent 的列表式 profile 继续通过 `compile_model_plugins` 在启动前把模型配置展开为普通 Rust 插件节点。此入口与默认应用 JSON 合同入口共用同一 `ModelService` / Rust 注册表算法。`models.service` 只提供中立注册表，`models.budget` 拥有唯一共享物理账本，每个配置的提供商 endpoint 对应一个实例插件，拥有自己的 HTTP 客户端和多个模型路由。消费方通过路由标记声明真实依赖，提供商替换会重建受影响的消费方，其他提供商和预算资源保持原代次。提供商的路由注册使用自身 Rust 身份与租约；卸载先排空调用，再移除路由和关闭客户端。

原配置中的服务实例 ID 留给预算节点，因此现有 `KB_STORAGE_CONFIG` 的预算消费绑定不变；注册表使用派生 ID。独立使用 `PluginHost` 时，先调用 `compile_model_plugins(profile, catalog)` 再构造宿主。直接注入借用服务或客户端仍受支持。`from_config` 和 `from_clients` 是显式独立构造入口，不再由已托管的公共服务内部调用。`from_config` 返回的服务拥有内部创建的预算账本，先关闭全部模型适配器再关闭账本；适配器清理失败时保留账本，沿用现有关闭重试。

保持依赖图形状的凭据、端点和模型配置变更可替换相应实例。增删提供商或路由、更改 Embedding 维度需要重新编译组合并启动，原图会明确返回 `MODEL_PROVIDER_GRAPH_RECOMPILE_REQUIRED`。

`requires: ["models"]` 取得注册表，提供商本身也使用这个依赖注册路由。在挂载函数中立即绑定路由的第三方插件，还须声明相应的 `models.route.generation`、`models.route.embedding` 或 `model_route_marker(name)` 返回的具名路由依赖。这样 Rust 在目标提供商就绪后才挂载消费方，并只在该提供商变更时重建它。只需在应用启动后动态查询注册表的插件可以仅依赖 `models`。内置视图由配置编译器添加对应路由依赖。

## 公共 SDK 与准入合同

`model_sdk` 是独立公共协议模块，只依赖中立错误类型与标准库签名校验，不导入宿主、知识库、Agent 或供应商 SDK。
`ModelInfo.protocol` 显式声明 `model-service/1`；provider、model、能力、输入模态和实现/端点修订均属于注册信息；`ModelRequest` 包含能力、
能力专属 payload 和调用上下文；`ModelResult` 包含提供商身份、结果、可选 usage 和请求 ID。
`Media` 传递有限大小的 bytes 与 MIME；图片 URL 仅作为结果引用，不自动下载。

| 能力 | 输入 payload | 结果 data |
| --- | --- | --- |
| `chat` | `messages`，可选 `max_tokens` | `text`、`finish_reason` |
| `embedding` | `texts`、`task=document/query`、可选 `dimensions` | 按输入顺序返回的 `vectors` |
| `ocr` | 图片 `media`，可选指令与领域 validator | 识别对象；知识库仍校验 blocks、完整性与坐标 |
| `transcription` | 音频 `media`，可选 `language` | `text` |
| `speech` | `text`，可选 `voice` | 音频 `media` |
| `image` | `prompt`，可选 `count`、`size` | `images`，含媒体或 HTTPS URL |

聊天目前支持 system/user/assistant 文本；原生 tool-call、流式、图像编辑、实时音频和异步
生成作业未实现。`streaming=False`，未支持的请求选项明确拒绝；chat 消息对象只接受 `role` 和文本 `content`，不透传未准入的嵌套工具或媒体字段。工具决策继续通过结构化
文本合同完成，不能把这些能力宣传为提供商原生工具协议。

注册时验证协议版本、`invoke(request)` 的同步调用签名，以及自有资源的 `close()`。
异步函数、生成器、继承但未实现的 Protocol 方法、额外必填参数和未知版本均拒绝，失败不发布路由。
`close()` 必须同步完成并返回 `None`；返回未执行的协程等值保持清理失败，不能报告已卸载。
存在 `completion_port()` 扩展时也检查工厂签名；绑定结构化服务时验证返回端口的方法与预算参数。
这些检查不执行试探性模型请求，也不证明任意插件代码的语义正确性；结果仍在每次调用边界检查。

请求先校验能力与参数再发送；结果校验身份、向量形状和媒体类型。不同能力不强制使用
同一个聊天响应格式。注册返回 disposer；调用持有租约，活跃调用期间不能卸载或关闭。
结构化绑定固定一次注册记录，卸载重注册后旧绑定失效，即便复用同一适配器对象也不能继续调用。

Embedding 和 OCR 消费者使用 `models.bind(route, capability)` 固定一次注册。每次调用都重新
取得租约；即便同一个适配器对象被卸载后重新注册，旧绑定也失效。向量索引的 manifest 不会
在路由替换后继续标注旧模型。需要重新装配消费者，并根据实际模型身份决定是否重建索引。

## 提供商实现与计量

| 适配器 | 当前支持 |
| --- | --- |
| `LunaAdapter` | 原有 Luna 文本和视觉 OCR；继续使用原 SDK、缓存、重试与账本路径 |
| `EmbeddingAdapter` | 原有 Google / Vertex Embedding 对象的通用能力包装 |
| `OpenAICompatibleAdapter` | Chat Completions、embeddings、视觉 OCR、audio transcriptions、audio speech、images generations |
| `AnthropicAdapter` | 原生 Messages 文本协议，转换为同一 chat 结果 |

OpenAI 兼容服务仍须实际支持所声明的端点、模型和参数。一个 provider 可以注册多个具名
模型路由；不同供应商也可以同时注册。适配器没有静默跨供应商回退。

新 HTTP 适配器复用 `rag_testing.budget.BudgetLedger` 的持久事务实现，每次 HTTP 发送前
预留，关闭 SDK/HTTP 自动重试和重定向。所有路由共享配置指定的同一账本。读超时等未知
结果保留 UNKNOWN 并阻止后续发送；HTTP 错误保留 ERROR。收到 403 后使用现有
`ProviderAdmission` 持久封锁该提供商/模型，不能重启绕过。

示例按请求数限制预算，不表示货币或精确 token 成本。`reservation` 必须覆盖账本中所有
资源；可以配置保守单位上限，但当前新适配器不会用厂商 token usage 自动折算这些单位。
缺失 usage 为 None，所有未结算单位保留预留值，不能当作免费。已知未发送的连接失败或
发送前取消、过期会释放相应预留。适配器在预留完成、真正发送前重新检查绝对截止时间，
并更新 HTTP 超时；独立结构化调用不依赖外层执行上下文也遵守该限制。结构化 `AgentModel` 的调用/token 准入与物理账本保持不同职责。

每次发送记录 `model-calls/<physical_id>.json`，包含最终请求体哈希、逻辑调用标识、状态及
HTTP 状态，不保存密钥、认证头或媒体原文。新适配器不缓存响应；原有 Luna 精确缓存行为
保留。模型结果的 usage 是厂商报告值，不等同于费用结算。

## 配置与产品接入

独立进程插件可在 manifest 声明 `requires: ["llm"]`，通过依赖回调调用
`llm.complete_json(messages, role=..., schema=..., call_context=...)`。该方法调用原有
`complete()` 并将 `CompletionEnvelope` 转成 JSON 对象，保留 value、request_id、usage、
attempts、finish_reason、cache_kind 和 request_hash，不另建模型路由或账本。
Python 插件继续使用返回类型化结果的 `complete()`。进程接口仅传递 JSON 参数，不能传递
Python validator 回调或任意对象。完整示例见 `examples/process-llm/`，回调协议见
[插件运行时](plugin-runtime.md)。

复制 `config/models.example.json` 到私有 `.local/models.json`，按供应商真实能力填写模型名，
删除不用的路由，并设置 `api_key_env` 指向的环境变量。示例模型名不代表账户已获准调用。

```bash
export KB_REMOTE_MODELS=1
export KB_MODELS_CONFIG=.local/models.json
uv run --no-sync cargo run --locked --offline -p agent-harness --bin evidence-host -- --profile config/host-profiles/api.json --stop-file .local/api.stop
```

知识库默认使用 `generation`，可选 `embedding` 与 `ocr` 路由。Embedding 配置还需给出
`dimension`。提供商 ID 仍必须符合文档的处理授权策略；更换向量模型或端点修订会改变
新配置的索引身份，已有索引需要显式重建，不能继续冒用旧向量。

不设置 `KB_MODELS_CONFIG` 时，原 `.local/providers.json` 配置继续工作：根组合创建原有
客户端并注册公共服务，Rust 管理模型路由的生命周期。文档 Embedding/OCR 和知识库生成通过公共服务访问。原有
Embedding manifest 保留，避免仅包装接口就使旧索引失效。

固定交付工厂仍要求冻结的旧提供商配置；它现在在应用创建时注入已选客户端，再安装现有
物理计量，不再读取或修改 `agent.model.provider.client`。不支持用新的多模态配置绕过该
固定交付准入。独立的冻结 Google 语义页索引/胶囊路径仍有自己的来源合同，不因新增
公共模型注册表就自动迁移模型、向量或历史预算。

通用 Agent 可选择 `config/agent-profiles/core-models.json`。它加载 `models.service`、
`models.structured` 和 `harness.model.service`，通过显式依赖取得 `llm`；不加载知识库。
默认 callable core profile 仍保留给无网络函数式 Agent。

## 其他插件与 SDK

### 当前文档处理能力

只需要文档识别和向量检索时，使用 [文档模型配置示例](../config/models.documents.example.json)，
复制到私有 `.local/document-models.json` 并设置 `KB_MODELS_CONFIG` 指向它。可以只保留
`embedding`、只保留 `ocr`，或者两者同时配置；无需配置 `generation`。应用仍提供解析、
索引和检索入口，但没有生成模型时不启动知识库问答 Agent，问答接口返回未配置状态。
文档的模型处理授权、提供商允许列表和索引身份校验继续生效。

独立插件组合可以使用以下 profile，通过 `PluginHost(profile, model_plugins(models))` 装配：

```json
{
  "version": 1,
  "plugins": [
    {"id": "models", "use": "models.service"},
    {"id": "embedding", "use": "models.embedding", "config": {"dimension": 768}},
    {"id": "ocr", "use": "models.ocr"}
  ]
}
```

业务插件声明依赖 `embedding` 或 `ocr`，无需加载 `llm`、Agent 或知识库插件。
`embedding.documents/query` 和 OCR 文档处理端口保留现有调用合同；专用 OCR SDK 可以实现
公共 `ModelAdapter`，返回同一识别合同，不要求该 SDK 提供聊天能力。原文、识别完整性、
坐标、证据和快照校验仍属于文档处理层。

OCR 报告以 `model_call_observation` 区分提供商请求统计和逻辑调用数。未提供物理统计的
SDK 仅报告 `logical_invocations`，实际物理调用以提供商账本为准；新 HTTP/OCR 端口
不执行格式修复重试，报告的 `maximum_format_corrections` 为 0。

Embedding 的 `ModelInfo.preferred_batch_size` 用于任务调度；HTTP 配置中的 `batch_size`
可显式设置，默认 1。这是调度批量大小，不表示按请求数配置的预算能精确计量 token。
原 Google 适配器声明 Gemini 批量为 16、Vertex 为 1，调度器不再判断具体供应商类型。

公共端口将模型错误映射为 `EmbeddingError`，保留 HTTP 状态、Retry-After 和物理请求 ID，
交由已有任务调度器决定如何处理。预算不足或共享账本存在未决请求时，HTTP 适配器在
发送前抛出 `ModelPreflightError`，包装层保留明确的预检分类。任务进入 RETRY，不消耗
重试次数、不增加供应商熔断计数；解除阻塞后可续建。普通异常文本不能证明尚未发送，
读超时的 UNKNOWN 不自动重发。任务 request_id 和授权
上下文哈希关联到物理请求记录，不记录授权原文。新的 HTTP 适配器仍不提供可供中断恢复
的向量捕获文件；不能仅凭请求哈希自动恢复 UNKNOWN，冻结 Google 语义页的恢复合同保持不变。

### 自定义供应商

消费者声明依赖 `models`，然后使用公共合同：

```python
from kb_platform.model_sdk import Capability, ModelRequest

models = ctx.require('models')
result = models.invoke('voice', ModelRequest(
    Capability.SPEECH, {'text': '你好', 'voice': 'alloy'},
    {'call_id': 'speech-001', 'purpose': 'notification', 'remaining_seconds': 30},
))
audio_bytes = result.data['media'].data
```

只需要结构化文本的辅助插件声明依赖 `llm`，直接调用 `ctx.require('llm').complete(...)`，
与知识库的规划、回答和核验共享同一个模型端口，不必经过主 Agent loop。

新供应商 SDK 插件实现 `ModelAdapter.info` 和 `invoke(ModelRequest) -> ModelResult`，
并通过可信 catalog 注册。它负责把 SDK 请求、结果、错误和 usage 转换到公共合同：

```python
def mount_provider(ctx, config):
    adapter = MySDKAdapter(config)  # 可信代码；SDK、认证和传输仅在此层
    try:
        dispose = ctx.require('models').register('my-voice', adapter, owned=True)
    except BaseException:
        adapter.close()
        raise
    ctx.defer(dispose)
```

插件需声明 `requires=('models',)`；只注册模型能力可以不提供额外服务。只实现公共 `info` 和
`invoke` 的 chat 适配器即可通过 `models.structured(route)` 服务结构化消费者。`model_completion`
把公共请求/结果接到现有 AgentModel 的 JSON Schema 校验与逻辑预算，不重新实现结构化生成。
同一注册的租约、结果身份、内容合同和重注册失效规则仍有效。标准 chat 结果须返回
`finish_reason='stop'` 才能完成结构化回答。

`completion_port()` 是可选扩展，供 Luna 等保留已有缓存、重试和物理计量；显式扩展端口经验证后原样保留，不按具体类型重新包装或丢失其缓存、预算声明。公共 HTTP 适配器直接使用标准结构化桥接，经过 `ModelService` 结果校验。通用桥接不实现供应商缓存或自动重试。
第三方调用抛出异常但没有明确发送状态时，逻辑预算保守记为已调用、usage 未知；确定未发送
应抛出 `ModelPreflightError` 或保留既有 `model_sent=False` 分类。不能把未观测状态当作零费用。
Luna OCR 无法按单次请求设置 `max_tokens`，因此明确拒绝该选项，不再静默忽略。
SDK 插件必须接入既有物理账本，并声明/约束 SDK 内部重试，不能仅在外层记一次费用。
注册表是可信插件的工程边界，不是阻止恶意插件自行联网的安全沙箱。

## 验证

`model_protocol_contracts.py` 使用合成适配器验证协议版本、同步签名、资源所有权、清理完成、
重复路由、结构化桥接、绑定失效、结果身份、未知 usage 和明确未发送分类，新增远程调用为零。
上述准入原则参考 [DSH 适配器义务](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/cookbook/adding-an-llm-adapter.md)：
能力和选项显式拒绝、路由唯一、生命周期约束和共享消费者协议。此处仍是同步 v1，不声称支持 DSH SSE/stream chunk 协议。

`model_service_contracts.py` 使用真实 HTTP 编码和合成 transport 验证六类能力、两个厂商
协议、统一结构化消费者、插件共享服务、权限封锁、UNKNOWN、预算拒绝、租约和配置。
统一入口仍为 `uv run --no-sync python scripts/check_project.py`。离线通过不代表真实供应商
的模型可用性、识别质量或线上费用验证。

新增边界合同使用真实 HTTP 适配器、SQLite 账本和任务调度控制逻辑，HTTP、时钟和 PostgreSQL
事务使用合成替身，验证准入拒绝与续建、注册身份及发送前截止时间；不代表真实供应商或数据库验收。
