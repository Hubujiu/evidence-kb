# 易失阶段缓存

`kb_platform.rag_testing.cache.StageCache` 支持持久对象和易失 KV 两种 payload 后端。二者互斥。未传入 `payload_cache` 时，原有本地文件或对象存储行为保持不变。

易失模式要求外部注入关系索引和实现 `storage.kv.expiring/v1` 的句柄。

```python
cache = StageCache(
    database=index,
    payload_cache=kv,
    payload_ttl_seconds=300,
)
```

关系索引保存完整 `StageKey` 的哈希到 payload 内容哈希的映射。KV 以内容哈希为键，保存 JSON bytes。`payload_ttl_seconds` 默认 300 秒，范围为大于零且不超过 365 天。

## 读取与重建

每次 `get` 先执行当前授权、执行模式和精确关系索引查询。`cold` 及 `live` 的 `under_test=True` 返回未命中。命中 KV 后重新解析完整 JSON，并核对内容哈希。TTL 不参与复用判定。

过期、清空和 KV 后端不可用返回 `None`。损坏、非法编码及内容哈希不匹配报 `ValueError('cache object integrity failure')`。关系索引读取错误直接传播。

`put` 先提交精确索引，再写入带 TTL 的 payload。KV 写入失败允许索引保留，此时后续读取未命中。相同阶段键只能重新写入同一输出，输出改变仍报错，即使旧 payload 已过期。

缓存未命中后，现有调用方决定是否重新执行阶段。既有预算、物理请求记录及未知费用约束继续生效。此缓存不能代替账本、会话或不可变证据主存储。

## 插件接口

`kb_platform.storage_cache_plugins.stage_cache_plugins()` 导出 `storage.cache.volatile`，提供 `stage.cache` 服务。

| 槽位 | 所需合同 |
|---|---|
| `index` | `storage.relational/v1` |
| `payload` | `storage.kv.expiring/v1` |

插件配置仅接受可选的 `payload_ttl_seconds`。依赖装配使用 `compile_storage_plugins`，具体提供商由实例和槽位配置选择。索引表结构由 `StageCache` 管理，连接生命周期由提供商和宿主管理。调用 `StageCache.close()` 不关闭注入的数据库或 KV。

## 验证入口

离线合同位于 `kb_platform/rag_testing/volatile_stage_cache_contracts.py`，使用真实 SQLite、Rust 宿主和内存 TTL 夹具，覆盖精确依赖、授权、完整性、缓存失效、重建与预算限制。

现有 `storage_contract_integration.suite` 包含真实 Redis 分别搭配 SQLite、PostgreSQL、MySQL 的阶段缓存用例。真实 TTL 和清空以本轮专属命名空间执行。网络错误使用故障注入，记录中明确标注。清理验证该命名空间无残留键。

这些用例接入统一项目检查，不产生模型、Embedding 或裁判调用，也不用于性能比较。
