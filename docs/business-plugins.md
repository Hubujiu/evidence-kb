# 内置业务插件参考

`platform_plugins.py` 提供基础设施 catalog，`model_plugins.py` 提供共享模型 catalog，`knowledge_profile.py` 提供知识库 catalog。`application_composition.py` 读取配置并组合目录。算法继续由现有文档、索引、检索、证据、会话和 workflow 模块实现。

## 显式构造端口

Platform 接收 `repository`、`search`、`store`、`parser`、`embedding_jobs`、`embedding_provider` 和 `ocr_client`。PostgresRepository 管理每次领域事务的连接。Platform 保留权限检查、authority 锁、revision 复查和 snapshot 提交。

KnowledgeService 接收显式会话存储、gateway、model、context policy、context manager、runtime、workflow factory、tool executor、semantic pages 和检索配置。它不创建第二个存储、运行时或模型实例。`create_knowledge_application()` 可借用已有 Platform、ResearchService 和模型服务，借用路径不关闭这些对象。

内置插件的配置 Schema 在候选 profile 和单插件更新入口校验。目录、模型路由、维度、重试次数、上下文预算和检索策略的无效值在排空旧实例前拒绝。涉及跨字段预算的条件复用纯值合同，不通过创建业务资源试探配置是否合法。

`read_model_config()`、`read_provider_config()` 和 `read_semantic_config()` 是引用文件的只读校验入口，激活时也复用它们。已有预算 caps 用只读 SQLite 连接核验。文件依赖哈希由 Rust 用于配置变化判定和挂载前核对，不保存密钥内容，也不把可变请求账本的全文件哈希当作配置依赖。

## 服务与资源归属

| 工作包与插件 | 服务与依赖 | 资源与关闭方式 | 停用后仍可用的能力 |
| --- | --- | --- | --- |
| B0 `platform.service` | `platform`，依赖全部显式基础端口 | 无额外连接。通过根宿主租约调用 | 独立原生执行或通用 Agent |
| B1 `models.service` | `models` | 只关闭自有适配器。失败项保留供重试 | 原生解析与 BM25 |
| B1 `models.route.generation` | generation 路由，依赖注册表和客户端 | 独立注册身份及所属实例租约 | Embedding 路由 |
| B1 `models.route.embedding` | embedding 路由，依赖同上 | 独立注册身份及所属实例租约 | generation 与 OCR |
| B1 `platform.model_clients.remote` | `model.clients` | 关闭自己创建的客户端，显式传入时借用 | 本地能力 |
| B2 `platform.repository.postgres` | `platform.repository` | 每次事务关闭连接 | 不依赖数据库的能力 |
| B2 `platform.search.postgres` | `platform.search`，依赖 repository | 关闭自己的连接池 | 其他搜索实例 |
| B2 `platform.search.embedded` | `platform.search` | 释放自己的索引 reader | 同上 |
| B2 `platform.search.opensearch` | `platform.search` | 复用现有按请求访问实现 | 同上 |
| B2 `platform.originals.files` | `platform.originals` | 卸载不删除业务数据 | 数据库中的证据快照 |
| B2 `harness.store.sqlite` | `session.store` | 短事务结束时关闭 SQLite 连接 | 其他组合的独立会话 |
| B3 `platform.parser.native` | `platform.parser` | 大文件沿用受控解析子进程及超时 | 移除 OCR 后仍可原生解析 |
| B3 `models.ocr`、`platform.ocr.client` | `ocr`，依赖所选模型路由 | 借用模型绑定 | Embedding。独立 OCR 路由也不影响 generation |
| B4 `platform.embedding_jobs` | `platform.embedding_jobs`，依赖 repository | 复用持久任务状态机，不启动额外轮询线程 | BM25 |
| B4 `knowledge.semantic` | `knowledge.semantic`，依赖 platform 与 gateway | 关闭自己创建的语义页 provider | 原始证据与普通检索 |
| B5 `knowledge.reranker` | `knowledge.reranker`，依赖共享模型 | 借用同一逻辑预算和模型绑定 | `knowledge.reranker.none` 与 BM25-only 配置 |
| B5 `knowledge.gateway` | `knowledge.gateway` | 无额外存储，保留授权和最终请求绑定 | 独立 gateway |
| B6 `knowledge.tools.catalog` | `knowledge.tool_plugins` | 显式工具定义，effect 经过既有执行规则 | 不使用该工具的组合 |
| B6 `knowledge.executor` | `knowledge.executor` | 无额外资源，保留副作用重试规则 | 无工具能力 |
| B7 `harness.*` | runtime、prompt、context、model、sessions、tools 和 loop | 分别替换，持久会话使用一个显式 SessionStore | 独立知识库 workflow |
| B8 `knowledge.workflow.graph`、`knowledge.driver.graph` | `knowledge.workflow`、`loop` | 不创建外层规划器或临时会话 | 通用 Agent |
| B8 `knowledge.service`、`knowledge.application` | `knowledge`、`application` | 借用绑定服务，请求结束前保留依赖 | 独立文档、检索和模型能力 |

插件默认允许并发调用。领域事务、会话 revision 和持久任务租约继续控制业务并发。停止先拒绝新准入，再等待已有租约结束。同步提供者调用仍在调用线程执行，取消通知不代表函数已经停止。

## 失败与恢复

ingest 在解析前检查权限，解析后重新锁定 authority 并复查权限与 revision。替换 parser 不改变提交顺序。原文版本、snapshot、quote、anchor 和坐标继续由既有合同校验。

EmbeddingJobs 保留 COMPLETE、ERROR、RUNNING 和 UNKNOWN、物理请求身份及费用关联。卸载或重启不清零账本，也不自动重发 UNKNOWN。恢复使用原有 reconcile 和 run 协议。

delivery 在创建根组合前建立 CapturedLuna 和 Embedding 客户端，一次性传给模型插件。请求继续使用现有物理账本和采集器，避免先创建客户端再替换。ReviewedPredecessorLedger 的既有审批记录、未知费用和预算边界保留。

## 验证范围

`platform_plugin_contracts.py` 用真实 EmbeddedSearch 和受控响应的 OpenSearch 处理同一合成原文，并调用同一个 Platform 搜索方法，验证 profile 替换、调用参数和关闭归属。该合同不代表真实数据库或搜索服务验收。

`knowledge_profile_contracts.py` 保留默认图入口、请求控制和最终证据绑定回放。`knowledge_lifecycle_contracts.py` 保留持久会话 revision、已完成请求幂等和未知请求恢复。`model_service_contracts.py` 使用受控 HTTP transport 验证多个模型适配器、物理请求关联、文档模型独立运行和旧路由绑定失效。

统一入口执行当前评分。真实 PostgreSQL、外部解析器和供应商验收单独报告。
