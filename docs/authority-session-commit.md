# 授权与会话的共同提交

知识库成功回合使用同一 authority 数据集保存权限、发布指针和会话。`knowledge.sessions/2` 的 `finish` 必须提交本轮最初取得的 `scope_id`、`manifest_id`、`authority_revision`。内置 access、data 和 sessions 必须绑定同一个 authority 出口；旧主版本不能静默接入。

`KnowledgeSessions.finish` 在一个写事务中锁定 tenant authority 行，检查当前成员的 `kb.read`、完整授权快照、持久会话的租户/所有者/知识库以及写入租约，然后追加 assistant/完成事件、状态和幂等响应。数据库物理提交或回滚之前不释放锁。权限写入、来源策略及绑定变更也取得同一个 tenant 锁；发布操作虽然不增加 tenant revision，仍由 manifest 比较覆盖。

因此，先提交的撤权会使答案提交失败；先持锁的答案提交结束后，撤权才能提交。提交以后才发生的撤权不会逆向删除历史，但读取和幂等重放仍执行当前权限校验。数据库管理员直接修改表、恶意替换存储实现和最后一次执行检查之后的取消不在此保证内。

事务回执丢失保留 `COMMIT_UNKNOWN` 和 `operation_id`，不追加失败回合、不重跑生成。调用方使用原 session/request/fingerprint 查询恢复；已完成请求返回原响应，未确定完成的请求仍保留租约/未决状态。不能用新的请求身份掩盖未知提交。

通用 `SessionStore` 仍支持独立会话数据库。它只负责所有者、租约和原子持久化；知识库授权由领域适配器负责。SQLite 本地合同覆盖独立连接的撤权竞争、物理提交前后进程退出以及未知提交。其他后端必须满足相同的事务与行锁合同；有限 TLC 模型不是数据库实现的证明。

## 从旧布局迁移

旧 authority schema 会被拒绝启动。迁移不会原地修改旧库，也不会丢弃旧 session 库后启动空历史。停止旧应用的所有写入者并保留备份，再分别冻结导出 authority 和 sessions。若存在 WAL 文件，不能只复制主数据库文件作为备份；使用数据库一致性备份或停机并关闭连接后的完整数据集。

旧结构化 authority 使用现有迁移入口导出：

```bash
uv run --no-sync python scripts/migrate_knowledge_storage.py export \
  --source-format structured-authority-v1 --source-url-env OLD_AUTHORITY_URL \
  --source-application OLD_APP --source-owner OLD_OWNER --source-space OLD_SPACE \
  --expected-source-dataset OLD_DATASET_ID --source-frozen --output NEW_AUTHORITY_EXPORT_DIR

uv run --no-sync python -m kb_platform.session_transfer export \
  --root OLD_SESSION_ROOT --source-frozen --output NEW_SESSION_EXPORT_FILE
```

`OLD_AUTHORITY_URL` 是环境变量名，其值是旧数据库 URL；PostgreSQL 还需显式 `--source-namespace`。session CLI 的 `--root` 适用于原本地结构化会话库，旧单文件格式使用其 `--legacy-sqlite` 选项；其他存储可调用统一 `export_sessions` API。源身份、schema 哈希、记录完整性均须匹配，不能用文件时间或大小替代校验。

按存储初始化流程创建新的、尚未使用的结构化数据集，在新配置中记录它的 dataset ID，然后导入：

```bash
uv run --no-sync python scripts/migrate_knowledge_storage.py import \
  --input NEW_AUTHORITY_EXPORT_DIR/knowledge-export.json \
  --sessions-input NEW_SESSION_EXPORT_FILE --target-config NEW_STORAGE_CONFIG \
  --expected-target-dataset NEW_DATASET_ID --target-offline --output NEW_RECEIPT_DIR
```

两份导出在同一目标事务中导入，并逐表回读核验。请求 token、fingerprint、状态、完成响应、历史和工件原样保留；原发布指针保留，因此仍须绑定原对象与索引资源。重复导入仅在来源哈希和目标记录都一致时返回幂等回执，目标已有新工作时拒绝覆盖。预算和物理请求账本保持原绑定，不迁空、不清零；本命令不调用模型、Embedding 或 judge。

最后将 `knowledge.sessions/2` 与 access/data 绑定到同一 `knowledge.authority.storage/2` 出口，核对导入回执后再启动。旧配置及数据保留为停机回退依据；新旧写入者不能同时运行。新的默认低层 profile 已移除独立 `harness.store.sqlite`，知识库会话随平台 authority 数据集管理，`KB_AGENT_ROOT` 不再选择知识库会话位置。
