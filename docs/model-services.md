# 公共模型服务与提供商插件

`model_service.ModelService` 是提供商无关的能力注册表。知识库、通用 Agent 和辅助插件依赖
`models` 或结构化文本服务 `llm`；供应商 SDK、HTTP 格式和认证停留在适配器中。内部协议
为 `model-service/1`，采用同步请求/响应。模型能力是显式声明，不因接口兼容而推定支持。

参考 DSH 固定提交 [f3803c9 的 LLM 注册与调用边界](https://github.com/deepseek-ai/deepseek-harness/blob/f3803c96974ad4a94195fb97e5c637e9c2a3afe3/packages/llm/llm/src/index.ts)。
本实现借鉴其注册、绑定和共享服务方式，不依赖 Cordis，也不声称兼容 DSH 插件或流式协议。

## 内部合同

`ModelInfo` 声明 provider、model、能力、输入模态和实现/端点修订；`ModelRequest` 包含能力、
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
生成作业未实现。`streaming=False`，未支持的请求选项明确拒绝。工具决策继续通过结构化
文本合同完成，不能把这些能力宣传为提供商原生工具协议。

请求先校验能力与参数再发送；结果校验身份、向量形状和媒体类型。不同能力不强制使用
同一个聊天响应格式。注册返回 disposer；调用持有租约，活跃调用期间不能卸载或关闭。
结构化绑定固定适配器实例，卸载重注册后旧绑定失效，不能静默切换模型。

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
缺失 usage 为 None，所有未结算单位保留预留值，不能当作免费。只有已知未发送的连接失败
可释放相应预留。结构化 `AgentModel` 的调用/token 准入与物理账本保持不同职责。

每次发送记录 `model-calls/<physical_id>.json`，包含最终请求体哈希、逻辑调用标识、状态及
HTTP 状态，不保存密钥、认证头或媒体原文。新适配器不缓存响应；原有 Luna 精确缓存行为
保留。模型结果的 usage 是厂商报告值，不等同于费用结算。

## 配置与产品接入

复制 `config/models.example.json` 到私有 `.local/models.json`，按供应商真实能力填写模型名，
删除不用的路由，并设置 `api_key_env` 指向的环境变量。示例模型名不代表账户已获准调用。

```bash
export KB_REMOTE_MODELS=1
export KB_MODELS_CONFIG=.local/models.json
uv run uvicorn kb_platform.api:app --host 127.0.0.1 --port 58080
```

知识库默认使用 `generation`，可选 `embedding` 与 `ocr` 路由。Embedding 配置还需给出
`dimension`。提供商 ID 仍必须符合文档的处理授权策略；更换向量模型或端点修订会改变
新配置的索引身份，已有索引需要显式重建，不能继续冒用旧向量。

不设置 `KB_MODELS_CONFIG` 时，原 `.local/providers.json` 配置继续工作：应用先构造原有
客户端，再注册公共服务，文档 Embedding/OCR 和知识库生成通过公共服务访问。原有
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
交由已有任务调度器决定如何处理。读超时的 UNKNOWN 不自动重发。任务 request_id 和授权
上下文哈希关联到物理请求记录，不记录授权原文。新的 HTTP 适配器仍不提供可供中断恢复
的向量捕获文件；不能仅凭请求哈希自动恢复 UNKNOWN，冻结 Google 语义页的恢复合同保持不变。

### 自定义供应商

消费者声明依赖 `models`，然后使用公共合同：

```python
from kb_platform.model_service import Capability, ModelRequest

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

插件需声明 `requires=('models',)`；只注册模型能力可以不提供额外服务。需要供结构化 LLM
使用的 chat 适配器还实现 `completion_port()`，满足现有 `ModelProvider` 的调用与计量合同。
SDK 插件必须接入既有物理账本，并声明/约束 SDK 内部重试，不能仅在外层记一次费用。
注册表是可信插件的工程边界，不是阻止恶意插件自行联网的安全沙箱。

## 验证

`model_service_contracts.py` 使用真实 HTTP 编码和合成 transport 验证六类能力、两个厂商
协议、统一结构化消费者、插件共享服务、权限封锁、UNKNOWN、预算拒绝、租约和配置。
统一入口仍为 `uv run --no-sync python scripts/check_project.py`。离线通过不代表真实供应商
的模型可用性、识别质量或线上费用验证。
