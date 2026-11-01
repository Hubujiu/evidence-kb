# 当前插件目录

本文件由 `uv run --no-sync python -m scripts.generate_plugin_catalog --output docs/plugin-catalog.md` 生成。

当前共有 72 个静态内置实现键。统计只构造 PluginSpec，不挂载宿主、不连接后端、不调用模型。

模型提供商实例、存储实例、授权绑定和消费方包装按配置动态生成；外部安装的进程插件另计。公共服务可选，第三方可以自建服务和适配器。

| 类别 | 数量 | 源码 |
| --- | ---: | --- |
| 通用 Agent | 16 | [harness_services.py](../kb_platform/harness_services.py) |
| 公共模型服务 | 9 | [model_plugins.py](../kb_platform/model_plugins.py) |
| 平台基础设施 | 27 | [platform_plugins.py](../kb_platform/platform_plugins.py) |
| 知识库问答 | 18 | [knowledge_profile.py](../kb_platform/knowledge_profile.py) |
| 公共存储服务 | 1 | [storage_plugins.py](../kb_platform/storage_plugins.py) |
| 易失阶段缓存 | 1 | [storage_cache_plugins.py](../kb_platform/storage_cache_plugins.py) |

提供商声明的服务合同如下。它们是适配器类型，不额外增加静态 PluginSpec 计数。

| 提供商 | 服务出口及合同 |
| --- | --- |
| postgresql | relational: storage.relational.postgres/v1; kv: storage.kv/v1, storage.kv.durable/v1; document: storage.document/v1; vector: storage.vector/v1; lexical: storage.lexical.bm25/v1 |
| mysql | relational: storage.relational/v1; kv: storage.kv/v1, storage.kv.durable/v1 |
| sqlite | relational: storage.relational/v1; kv: storage.kv/v1, storage.kv.durable/v1; document: storage.document/v1 |
| redis | kv: storage.kv/v1, storage.kv.expiring/v1 |
| files | object: storage.object/v1 |
| s3 | object: storage.object/v1 |
| embedded | vector: storage.vector/v1; lexical: storage.lexical.bm25/v1 |

配置、显式初始化和迁移见 [存储服务](storage-plugins-design.md)。

| 类别 | 实现键 | 提供服务 | 静态依赖 | 存储槽位 |
| --- | --- | --- | --- | --- |
| 通用 Agent | `harness.application` | application | sessions, loop | 无 |
| 通用 Agent | `harness.compaction.recent` | compaction | 无 | 无 |
| 通用 Agent | `harness.context.history` | prompt | context.policy, compaction, model | 无 |
| 通用 Agent | `harness.context.policy` | context.policy | 无 | 无 |
| 通用 Agent | `harness.loop` | loop | runtime, sessions, tools | 无 |
| 通用 Agent | `harness.loop.model-tools` | loop | runtime, sessions, tools, prompt, model | 无 |
| 通用 Agent | `harness.model.callable` | model | 无 | 无 |
| 通用 Agent | `harness.model.service` | model | llm | 无 |
| 通用 Agent | `harness.model.structured` | model | 无 | 无 |
| 通用 Agent | `harness.prompt.history` | prompt | 无 | 无 |
| 通用 Agent | `harness.runtime` | runtime | 无 | 无 |
| 通用 Agent | `harness.sessions.durable` | sessions | session.store | 无 |
| 通用 Agent | `harness.sessions.memory` | sessions | 无 | 无 |
| 通用 Agent | `harness.store.database` | session.store | 无 | 无 |
| 通用 Agent | `harness.store.sqlite` | session.store | 无 | 无 |
| 通用 Agent | `harness.tools` | tools | 无 | 无 |
| 公共模型服务 | `models.budget` | models.budget | 无 | 无 |
| 公共模型服务 | `models.clients` | model.clients | 无 | 无 |
| 公共模型服务 | `models.embedding` | embedding | models | 无 |
| 公共模型服务 | `models.ocr` | ocr | models | 无 |
| 公共模型服务 | `models.route.client` | 无 | models, model.clients | 无 |
| 公共模型服务 | `models.route.embedding` | models.route.embedding | models, model.clients | 无 |
| 公共模型服务 | `models.route.generation` | models.route.generation | models, model.clients | 无 |
| 公共模型服务 | `models.service` | models | 无 | 无 |
| 公共模型服务 | `models.structured` | llm | models | 无 |
| 平台基础设施 | `platform.borrowed` | platform | 无 | 无 |
| 平台基础设施 | `platform.capabilities.embedding` | platform.embedding, platform.ocr | embedding | 无 |
| 平台基础设施 | `platform.capabilities.models` | platform.embedding, platform.ocr | embedding, ocr | 无 |
| 平台基础设施 | `platform.capabilities.none` | platform.embedding, platform.ocr | 无 | 无 |
| 平台基础设施 | `platform.capabilities.ocr` | platform.embedding, platform.ocr | ocr | 无 |
| 平台基础设施 | `platform.embedding.client` | embedding | models, model.clients, models.route.embedding | 无 |
| 平台基础设施 | `platform.embedding_jobs` | platform.embedding_jobs | platform.repository | 无 |
| 平台基础设施 | `platform.model_clients.remote` | model.clients | 无 | 无 |
| 平台基础设施 | `platform.ocr.client` | ocr | models, model.clients, models.route.generation | 无 |
| 平台基础设施 | `platform.originals.files` | platform.originals | 无 | 无 |
| 平台基础设施 | `platform.originals.storage` | platform.originals | 无 | objects: storage.object/v1 |
| 平台基础设施 | `platform.parser.native` | platform.parser | 无 | 无 |
| 平台基础设施 | `platform.repository.postgres` | platform.repository | 无 | 无 |
| 平台基础设施 | `platform.repository.storage` | platform.repository | 无 | primary: storage.relational/v1 |
| 平台基础设施 | `platform.research` | research | platform, platform.embedding, models, knowledge.model | 无 |
| 平台基础设施 | `platform.research.borrowed` | research | 无 | 无 |
| 平台基础设施 | `platform.search.bm25.embedded` | platform.search.bm25 | 无 | 无 |
| 平台基础设施 | `platform.search.bm25.postgres` | platform.search.bm25 | 无 | 无 |
| 平台基础设施 | `platform.search.dense.embedded` | platform.search.dense | 无 | 无 |
| 平台基础设施 | `platform.search.dense.postgres` | platform.search.dense | 无 | 无 |
| 平台基础设施 | `platform.search.embedded` | platform.search | 无 | 无 |
| 平台基础设施 | `platform.search.opensearch` | platform.search | 无 | 无 |
| 平台基础设施 | `platform.search.postgres` | platform.search | platform.repository | 无 |
| 平台基础设施 | `platform.search.source.postgres` | platform.search.source | platform.repository | 无 |
| 平台基础设施 | `platform.search.split` | platform.search | platform.search.bm25, platform.search.dense, platform.search.source | 无 |
| 平台基础设施 | `platform.search.storage` | platform.search | 无 | bm25: storage.lexical.bm25/v1, dense: storage.vector/v1, source: storage.document/v1 |
| 平台基础设施 | `platform.service` | platform | platform.repository, platform.search, platform.originals, platform.parser, platform.embedding_jobs, platform.embedding, platform.ocr | 无 |
| 知识库问答 | `knowledge.application` | application | knowledge, knowledge.tools, loop | 无 |
| 知识库问答 | `knowledge.budget` | knowledge.budget | 无 | 无 |
| 知识库问答 | `knowledge.context` | knowledge.context | knowledge.context.policy | 无 |
| 知识库问答 | `knowledge.context.policy` | knowledge.context.policy | 无 | 无 |
| 知识库问答 | `knowledge.driver.graph` | loop | 无 | 无 |
| 知识库问答 | `knowledge.executor` | knowledge.executor | 无 | 无 |
| 知识库问答 | `knowledge.gateway` | knowledge.gateway | knowledge.platform, knowledge.research, knowledge.model, knowledge.reranker | 无 |
| 知识库问答 | `knowledge.model` | knowledge.model, llm | models, knowledge.context.policy, knowledge.budget | 无 |
| 知识库问答 | `knowledge.model.route` | knowledge.model, llm | models, models.route.generation, knowledge.context.policy, knowledge.budget | 无 |
| 知识库问答 | `knowledge.reranker` | knowledge.reranker | models, knowledge.model | 无 |
| 知识库问答 | `knowledge.reranker.none` | knowledge.reranker | 无 | 无 |
| 知识库问答 | `knowledge.resources` | knowledge.platform, knowledge.research | platform, research | 无 |
| 知识库问答 | `knowledge.semantic` | knowledge.semantic | knowledge.platform, knowledge.gateway | 无 |
| 知识库问答 | `knowledge.service` | knowledge | knowledge.platform, knowledge.research, knowledge.model, knowledge.store, knowledge.context.policy, knowledge.context, runtime, knowledge.gateway, knowledge.executor, knowledge.semantic, knowledge.workflow, knowledge.tool_plugins | 无 |
| 知识库问答 | `knowledge.sessions` | knowledge.store | session.store | 无 |
| 知识库问答 | `knowledge.tools` | knowledge.tools | knowledge | 无 |
| 知识库问答 | `knowledge.tools.catalog` | knowledge.tool_plugins | 无 | 无 |
| 知识库问答 | `knowledge.workflow.graph` | knowledge.workflow | 无 | 无 |
| 公共存储服务 | `storage.service` | storage.registry | 无 | 无 |
| 易失阶段缓存 | `storage.cache.volatile` | stage.cache | 无 | index: storage.relational/v1, payload: storage.kv.expiring/v1 |

关系方言约束与可选预算槽位由应用组合按配置声明，生成的真实依赖由 Rust 宿主检查。
合同 SDK 与注册器不强制第三方业务使用公共服务；模型和存储各自检查领域语义。
