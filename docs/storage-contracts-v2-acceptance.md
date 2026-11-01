# 六类存储合同验收记录

本轮按[批准方案](storage-contracts-v2-approval-plan.md)实现六类合同及消费者迁移。当前架构和实际支持范围见[存储设计](storage-plugins-design.md)，70 个静态内置插件的声明见[插件目录](plugin-catalog.md)。

## 已执行的验收

2026-11-01 本地统一入口 `.local/checks/storage-contract-final-20261101-04/summary.json` 的 12 个门禁通过，组件合同为 719 项。随后修复索引读者关闭竞争，统一入口 `.local/checks/storage-contract-final-20261101-05/summary.json` 的 10 个离线及受控进程门禁通过，组件合同增至 720 项，失败、错误、跳过、预期失败均为 0。真实资源在第四轮结束后清理，修改后的多数据库矩阵由 PR 必需 CI 再次执行。完整本地验收命令为：

```powershell
uv run --no-sync python scripts/check_project.py --local-integration --storage-integration --output <新目录>
```

| 验收层 | 结果和范围 |
| --- | --- |
| 原生与边界 | Rust format、Clippy、当前 release 制品、Rust tests、架构与 Ruff 全部通过 |
| 受控进程 | 独立 worker 的真实存储调用、重启恢复、有界 IPC 和资源排空通过 |
| PostgreSQL 运行时 | 2 项真实 HTTP、解析子进程、检索、引用和 ACL 测试通过 |
| 实际存储 | 冻结 plan 的 30 组全部通过，FAIL/ERROR/SKIP/NOT_RUN 均为 0 |
| 浏览器 | 存储矩阵中的 1 项上传、发布和 dense 引用流程通过；独立浏览器入口另有 2 项通过 |
| 独立验收 | 61 项合同与实际 SQLite 初始化、重启、删除重建、文档迁移及独立进程流程通过 |

实际后端包含 PostgreSQL、MySQL、SQLite、Redis、files 和 S3 兼容服务。会话测试覆盖三库及六个有向迁移；文档测试覆盖 PostgreSQL 与 SQLite 双向迁移及删除历史；对象测试覆盖 files/S3 双向复制、重复执行和损坏拒绝。78 项事务逃逸探针、权限撤销、失败与 UNKNOWN 费用保留分别记录在存储观察中。

BM25 与 dense 实际执行五种组合：同一 PostgreSQL 空间、两个独立 PostgreSQL 数据库、PG lexical/embedded vector、embedded lexical/PG vector、embedded 双通道。source 写入、单通道构建失败和权威原文损坏均不能发布半成品。HTTP 验证使用两个组合，权威文档使用 SQLite，原件使用 files/S3。

原始证据保留在统一 checker 输出的 `contracts.json`、`storage-integration/plan.json`、`storage-integration/report.json` 和 `runtime-integration/report.json`。独立浏览器报告为 `.local/storage-contract-browser-20261101-01/report.json`，独立验收为 `.local/storage-final-independent-20261101-01/report.json`。本地路径是本轮产物，复测必须写入新目录。

## 审查与失败记录

独立审查发现并修复三项问题：删除重建的文档复用 revision，进程写入已完成却因结果超限被误报，以及 `get_many` 的 SDK keyword 与实现不一致。两条 ABA 回归在修复前真实失败。当前删除保留 tombstone 并递增 revision，v2 快照保留删除历史。

第一次统一检查保留了架构声明、运行时账号和离线 CLI 合同启动子进程的失败。第二次检查保留了动态端口不符合既有运行时验收入口要求的失败。最后使用单独创建的固定端口验收库，以 `kb_app` 运行，未降低门槛。

远端首轮 CI 发现 MinIO 镜像无法公开拉取，以及 Windows 合同因落盘等待超时。MinIO 改为从[官方固定提交](https://github.com/minio/minio/tree/9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a)构建，源码 checksum、Go 1.24.8 镜像和模块均固定校验，运行时保留许可证。最后一轮 30 组真实矩阵使用这个新镜像通过。

Windows 合同使用 `RUNNER_TEMP` 下每轮独占目录，显式设置 Python 的三个临时目录环境变量，仍执行相同合同、FULL 持久化和原来的 180/170 秒门槛。CI 五轮交错的两盘写入及重开核验显示，该 runner 的临时盘比系统盘提交等待更短，因此没有保留“系统盘更快”的初始假设。没有采用诊断中变慢的 PERSIST/WAL 候选，也没有把池化后文件未释放的候选合入生产。这是验收环境配置，不是生产性能结论。最终远端结果以 [PR #25](https://github.com/Hubujiu/evidence-kb/pull/25) 的必需检查及其归档为准。

临时盘修正后，Windows 3.13 完整执行了 719 项合同，暴露出一个 Tantivy 目录清理竞争。不可变 generation 的创建和读取现在使用手动 reader，避免默认自动刷新的后台回调在关闭后继续访问元数据。新增回归真实执行 lexical、page、dense 查询，再关闭并立即删除四个 generation；相关 28 项合同及完整 720 项合同均在本地通过。reader 策略的语义见 [Tantivy 官方 API](https://tantivy-py.readthedocs.io/en/latest/api/tantivy/tantivy.html)。

跨家族审查后端不可用。本轮独立审查由另一 GPT 家族代理执行，未称作跨家族审核。

## 数据与资源边界

测试使用合成文档、本地模型响应和固定向量。新增外部模型、Embedding、judge 调用均为 0。评分器控制复用冻结 trace 并重新评分；它不代表企业生产 RAG 质量。本轮未报告数据库性能提升或费用节省百分比。

完整知识库关系仓储仍要求 PostgreSQL。公共 relational 合同不翻译原始 SQL，不提供跨服务分布式事务。更换绑定不自动迁移数据。当前 PG 搜索实现内部仍需 pgvector 和 pg_textsearch。

本轮四个专属容器及其卷、独立运行时验收库、源码构建的 MinIO 镜像、专属构建器及其下载缓存均已删除。五个本轮创建的独立开发 worktree 已在核对无未提交文件后移除。私有资源凭据文件已删除。共享 PostgreSQL 容器、共享镜像缓存和其他运行数据保持原状。

容器清理记录为 `.local/storage-contract-resources-20261101-01/cleanup.json`，运行时库清理记录为同目录的 `runtime-cleanup.json`，源码镜像构建与构建器清理见 `.local/minio-source-build-20261101-agent/verification.json`。保留验收报告和诊断证据。
