# 当前插件目录

本文记录当前工作区的静态内置实现和公开配置。清单由六个 catalog 工厂构造 `PluginSpec`，没有挂载插件、启动宿主、连接数据库或调用模型。它描述可选实现，不代表某个部署当前运行的实例。

当前共有 72 个静态内置实现键。数据库 route、bound、consumer 包装 spec 按部署配置动态生成，另计；外部安装的进程插件也不在此计数中。替代实现、借用对象和空能力占位都属于静态实现，不会在一个应用中同时全部加载。

| 类别 | 实现键数量 | 源码 |
| --- | ---: | --- |
| 通用 Agent | 16 | [harness_services.py](../kb_platform/harness_services.py) |
| 公共模型服务 | 8 | [model_plugins.py](../kb_platform/model_plugins.py) |
| 平台基础设施 | 24 | [platform_plugins.py](../kb_platform/platform_plugins.py) |
| 知识库问答 | 18 | [knowledge_profile.py](../kb_platform/knowledge_profile.py) |
| 公共数据库 | 1 | [database_plugins.py](../kb_platform/database_plugins.py) |
| 原件对象存储 | 5 | [blob_plugins.py](../kb_platform/blob_plugins.py) |

## 实现、实例、服务和提供商

`PluginSpec.key` 是实现键；profile 的 `id` 是部署实例名；`provides` 与 `requires` 是宿主服务依赖。`storage_slots` 声明消费者的数据库需求，组合器也为已有内置实现补充 slot 映射。编译后，消费者原有依赖加上对应 bound 服务。表中的静态 `requires` 不包含尚未绑定的数据库服务名。

`databases.service` 提供 `databases.registry`。PostgreSQL、MySQL、SQLite 是数据库提供商类，由配置选择并生成具名 route spec；没有三个额外的静态 `databases.provider.*` 实现键。多个 route 可以使用相同提供商类，数据空间按 application、owner、space 区分。

`models.service` 提供公共 `models` 注册表。`openai-compatible` 和 `anthropic` 是配置支持的适配器类型，不是两个同名 PluginSpec。provider 名和 generation 等模型路由名也不增加静态实现键计数。

`models.route.client` 只向模型注册表注册路由，没有额外宿主服务。`platform.capabilities.none`、`knowledge.reranker.none` 和未配置的 `knowledge.semantic` 仍参与装配，但可提供值为 None 的能力。

## 公开配置的静态展开

以下计数只展开 JSON bundle，不包含环境覆盖、数据库编译、检索拆分、模型注入或进程安装包。数据库配置会添加 registry、route、bound 和消费者包装；会话实例也可能从本地入口改为公共绑定。因此运行实例数不能直接等同于此表。

| 配置 | JSON 展开条目数 |
| --- | ---: |
| [config/application-profiles/bm25.json](../config/application-profiles/bm25.json) | 8 |
| [config/application-profiles/knowledge.json](../config/application-profiles/knowledge.json) | 27 |
| [config/application-profiles/native.json](../config/application-profiles/native.json) | 1 |
| [config/agent-profiles/core-models.json](../config/agent-profiles/core-models.json) | 12 |
| [config/agent-profiles/core.json](../config/agent-profiles/core.json) | 10 |
| [config/agent-profiles/knowledge.json](../config/agent-profiles/knowledge.json) | 19 |

默认根应用由 `application_composition.py` 选择：未启用 generation 时使用 bm25，启用时使用 knowledge。core 独立装配，不随知识库 HTTP 默认启动。`KB_DATABASES_CONFIG` 决定数据库绑定；`KB_SEARCH_CHANNELS_CONFIG` 可以将 search 替换为 split 并增加两个通道实例。

## 当前存储解耦边界

- 数据库注册、绑定、事务、迁移和连接生命周期已成为公共服务。任意可信插件可以声明 `storage_slots` 并使用 SDK，不需要依赖知识库。
- `harness.store.database` 提供同一 `session.store`，支持 SQLite、PostgreSQL、MySQL。`harness.store.sqlite` 保留本地入口，旧 SQLite 文件需要显式导入。
- `platform.repository.postgres` 使用公共 PG 绑定和知识库 schema。正文、ACL、发布和任务仍要求 PostgreSQL。保留的 DSN 装配入口内部创建公共绑定，不等于自动切换任意后端。
- `platform.search.split` 将 BM25 与 dense 分开配置；PG+PG 和 PG+embedded 是当前真实验收组合。
- `platform.originals.blobs` 使用公共对象绑定，files 与 S3 提供商负责原件。下载和预览通过对象接口。
- 缓存、预算和数据库回执支持三库；冻结历史、producer_guard sidecar、模型 audit JSON 及旧 Luna 缓存仍有明确的本地边界。

schema 与迁移属于消费数据库的业务实现，提供商不拥有文档表或会话表。详细接口、默认与显式绑定的差异见 [存储设计](storage-plugins-design.md)，验收和搬迁见 [执行方案](storage-plugins-execution-plan.md)。

## 完整静态目录

下表由实际 spec 枚举。空依赖不代表没有配置条件，例如借用对象、远程凭据和数据库 slot 仍需在装配时满足。

### 通用 Agent

| 实现键 | 提供服务 | 静态依赖服务 |
| --- | --- | --- |
| `harness.store.sqlite` | `session.store` | 无 |
| `harness.store.database` | `session.store` | 无 |
| `harness.sessions.durable` | `sessions` | `session.store` |
| `harness.context.policy` | `context.policy` | 无 |
| `harness.compaction.recent` | `compaction` | 无 |
| `harness.context.history` | `prompt` | `context.policy`, `compaction`, `model` |
| `harness.model.service` | `model` | `llm` |
| `harness.model.structured` | `model` | 无 |
| `harness.application` | `application` | `sessions`, `loop` |
| `harness.runtime` | `runtime` | 无 |
| `harness.sessions.memory` | `sessions` | 无 |
| `harness.prompt.history` | `prompt` | 无 |
| `harness.tools` | `tools` | 无 |
| `harness.model.callable` | `model` | 无 |
| `harness.loop` | `loop` | `runtime`, `sessions`, `tools` |
| `harness.loop.model-tools` | `loop` | `runtime`, `sessions`, `tools`, `prompt`, `model` |

### 公共模型服务

| 实现键 | 提供服务 | 静态依赖服务 |
| --- | --- | --- |
| `models.service` | `models` | 无 |
| `models.clients` | `model.clients` | 无 |
| `models.route.client` | 无 | `models`, `model.clients` |
| `models.route.generation` | `models.route.generation` | `models`, `model.clients` |
| `models.route.embedding` | `models.route.embedding` | `models`, `model.clients` |
| `models.structured` | `llm` | `models` |
| `models.embedding` | `embedding` | `models` |
| `models.ocr` | `ocr` | `models` |

### 平台基础设施

| 实现键 | 提供服务 | 静态依赖服务 |
| --- | --- | --- |
| `platform.model_clients.remote` | `model.clients` | 无 |
| `platform.embedding.client` | `embedding` | `models`, `model.clients`, `models.route.embedding` |
| `platform.ocr.client` | `ocr` | `models`, `model.clients`, `models.route.generation` |
| `platform.repository.postgres` | `platform.repository` | 无 |
| `platform.originals.files` | `platform.originals` | 无 |
| `platform.originals.blobs` | `platform.originals` | `blobs.bound` |
| `platform.parser.native` | `platform.parser` | 无 |
| `platform.embedding_jobs` | `platform.embedding_jobs` | `platform.repository` |
| `platform.search.postgres` | `platform.search` | `platform.repository` |
| `platform.search.embedded` | `platform.search` | 无 |
| `platform.search.bm25.postgres` | `platform.search.bm25` | 无 |
| `platform.search.bm25.embedded` | `platform.search.bm25` | 无 |
| `platform.search.dense.postgres` | `platform.search.dense` | 无 |
| `platform.search.dense.embedded` | `platform.search.dense` | 无 |
| `platform.search.split` | `platform.search` | `platform.search.bm25`, `platform.search.dense` |
| `platform.search.opensearch` | `platform.search` | 无 |
| `platform.capabilities.none` | `platform.embedding`, `platform.ocr` | 无 |
| `platform.capabilities.embedding` | `platform.embedding`, `platform.ocr` | `embedding` |
| `platform.capabilities.ocr` | `platform.embedding`, `platform.ocr` | `ocr` |
| `platform.capabilities.models` | `platform.embedding`, `platform.ocr` | `embedding`, `ocr` |
| `platform.service` | `platform` | `platform.repository`, `platform.search`, `platform.originals`, `platform.parser`, `platform.embedding_jobs`, `platform.embedding`, `platform.ocr` |
| `platform.borrowed` | `platform` | 无 |
| `platform.research.borrowed` | `research` | 无 |
| `platform.research` | `research` | `platform`, `platform.embedding`, `models`, `knowledge.model` |

### 知识库问答

| 实现键 | 提供服务 | 静态依赖服务 |
| --- | --- | --- |
| `knowledge.resources` | `knowledge.platform`, `knowledge.research` | `platform`, `research` |
| `knowledge.context.policy` | `knowledge.context.policy` | 无 |
| `knowledge.context` | `knowledge.context` | `knowledge.context.policy` |
| `knowledge.budget` | `knowledge.budget` | 无 |
| `knowledge.model.route` | `knowledge.model`, `llm` | `models`, `models.route.generation`, `knowledge.context.policy`, `knowledge.budget` |
| `knowledge.model` | `knowledge.model`, `llm` | `models`, `knowledge.context.policy`, `knowledge.budget` |
| `knowledge.sessions` | `knowledge.store` | `session.store` |
| `knowledge.workflow.graph` | `knowledge.workflow` | 无 |
| `knowledge.driver.graph` | `loop` | 无 |
| `knowledge.gateway` | `knowledge.gateway` | `knowledge.platform`, `knowledge.research`, `knowledge.model`, `knowledge.reranker` |
| `knowledge.reranker` | `knowledge.reranker` | `models`, `knowledge.model` |
| `knowledge.reranker.none` | `knowledge.reranker` | 无 |
| `knowledge.executor` | `knowledge.executor` | 无 |
| `knowledge.semantic` | `knowledge.semantic` | `knowledge.platform`, `knowledge.gateway` |
| `knowledge.service` | `knowledge` | `knowledge.platform`, `knowledge.research`, `knowledge.model`, `knowledge.store`, `knowledge.context.policy`, `knowledge.context`, `runtime`, `knowledge.gateway`, `knowledge.executor`, `knowledge.semantic`, `knowledge.workflow`, `knowledge.tool_plugins` |
| `knowledge.tools.catalog` | `knowledge.tool_plugins` | 无 |
| `knowledge.tools` | `knowledge.tools` | `knowledge` |
| `knowledge.application` | `application` | `knowledge`, `knowledge.tools`, `loop` |

### 公共数据库

| 实现键 | 提供服务 | 静态依赖服务 |
| --- | --- | --- |
| `databases.service` | `databases.registry` | 无 |

### 原件对象存储

| 实现键 | 提供服务 | 静态依赖服务 |
| --- | --- | --- |
| `blobs.service` | `blobs.registry` | 无 |
| `blobs.provider.files` | `blobs.route.files` | `blobs.registry` |
| `blobs.provider.s3` | `blobs.route.s3` | `blobs.registry` |
| `blobs.binding.files` | `blobs.bound` | `blobs.registry`, `blobs.route.files` |
| `blobs.binding.s3` | `blobs.bound` | `blobs.registry`, `blobs.route.s3` |

## 动态数据库 spec

`compile_database_plugins` 根据可信配置生成以下 spec。编码部分是配置身份的 UTF-8 十六进制表示，便于避免分隔符碰撞。

| 键形状 | 作用 |
| --- | --- |
| `databases.route.<route>` | 注册所选提供商和具名路由，依赖 registry |
| `databases.bound.<consumer>.<slot>` | 核对 route 注册，签发空间绑定，依赖 registry 和具体 route |
| `databases.consumer.<consumer>` | 包装原消费者，保留原业务 mount 并附加 bound 依赖 |

生成数量随路由、消费者及 slot 数变化。编译器同时拒绝未知消费者、缺失 slot、同空间路由冲突和重复键。更换 route 影响其依赖子图，旧注册句柄不会透明切换到新提供商。

## 独立进程插件示例

根装配从 `KB_PROCESS_PLUGINS_ROOT` 指定的安装目录追加 catalog，再由 profile 选择。仓库中的 manifest 是示例或监督夹具，文件存在不等于已经安装。进程消费者只能在活动 invoke 中回调已声明的依赖，参数和结果须符合 JSON 协议。公共数据库提供 `query_json` 与 `batch_json`，不提供跨 IPC 长事务令牌。

| manifest | 安装后的实现键 | 提供服务 | 依赖服务 |
| --- | --- | --- |
| [examples/process-blocked-pipe/plugin.json](../examples/process-blocked-pipe/plugin.json) | `process.blocked-pipe` | `external.blocked` | 无 |
| [examples/process-dependency/plugin.json](../examples/process-dependency/plugin.json) | `process.dependency` | `external.consumer` | `shared.model` |
| [examples/process-database/plugin.json](../examples/process-database/plugin.json) | `process.database` | `external.database` | `shared.database` |
| [examples/process-echo/plugin.json](../examples/process-echo/plugin.json) | `process.echo` | `external.echo` | 无 |
| [examples/process-llm/plugin.json](../examples/process-llm/plugin.json) | `process.llm-consumer` | `external.writer` | `llm` |
| [examples/process-supervision/plugin.json](../examples/process-supervision/plugin.json) | `process.supervision` | `external.supervision` | 无 |

## 清单复查代码

以下命令只构造 spec 并展开公开 profile，不读取私有模型配置、不挂载宿主、不运行配置检查回调。

```powershell
@'
from pathlib import Path
from kb_platform.harness_services import builtin_plugins
from kb_platform.model_plugins import model_plugins
from kb_platform.platform_plugins import platform_plugins
from kb_platform.knowledge_profile import knowledge_plugins
from kb_platform.database_plugins import database_plugins
from kb_platform.blob_plugins import blob_plugins
from kb_platform.plugin_profiles import expand_profile

catalogs = {'harness': builtin_plugins(), 'models': model_plugins(),
	'platform': platform_plugins(None), 'knowledge': knowledge_plugins(None, None),
	'databases': database_plugins(), 'blobs': blob_plugins()}
for group, specs in catalogs.items():
	print(group, len(specs))
	for spec in specs:
		print(spec.key, 'provides=', spec.provides, 'requires=', spec.requires,
			'storage_slots=', spec.storage_slots)
bundles = {'platform': 'config/application-bundles/platform.json',
	'knowledge': 'config/agent-bundles/knowledge.json', 'core': 'config/agent-bundles/core.json'}
for category in ('application-profiles', 'agent-profiles'):
	for path in sorted((Path('config') / category).glob('*.json')):
		print(path, len(expand_profile(path, bundles)['plugins']))
'@ | .venv\Scripts\python.exe -
```
