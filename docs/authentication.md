# 可替换认证会话

HTTP 业务通过 `authentication.authenticate(token) -> Actor | None` 获取身份，认证服务由插件宿主管理，每次请求取得调用租约。接口返回空值时响应 401；公共存储异常响应 503。认证会话与对话历史是两个不同领域：对话历史继续由 `SessionStore` 管理 revision、请求幂等和单写者事务。

API 默认装配 `authentication.file`，保留 `KB_IDENTITIES_FILE` 的部署方式。显式配置认证插件时，不再加载该文件，也不在认证失败后回退到文件 token。自定义认证服务必须实现同步的 `authenticate(token)`，不符合接口会阻止应用启动并清理已装配资源。

## Redis 或其他 TTL KV 提供商

应用 profile 增加以下条目，其他知识库插件和数据库绑定保持原配置：

```json
{"id": "authentication", "use": "authentication.sessions"}
```

在现有 `KB_STORAGE_CONFIG` 中增加 Redis 实例、服务和对应 binding：

```json
{
  "instances": {
    "auth-redis": {"provider": "redis", "config": {"url_env": "KB_AUTH_REDIS_URL"}}
  },
  "services": {
    "auth.sessions": {"instance": "auth-redis", "export": "kv"}
  },
  "bindings": {
    "authentication": {
      "sessions": {
        "service": "auth.sessions", "owner": "authentication", "space": "sessions",
        "cache_id": "authentication-sessions-v1"
      }
    }
  }
}
```

上面是需要合入现有 version 2 配置的片段，并非完整知识库存储配置。认证插件只声明 `storage.kv.expiring/v1`；更换提供商时不修改认证业务。`application_id`、owner、space、cache_id 隔离会话命名空间。Redis key 保存随机 bearer token 的 SHA-256 摘要，value 保存身份和绝对到期时间；Redis TTL 与业务到期检查同时生效，读取不会续期。

已验证用户身份的可信登录插件通过宿主调用 `issue(actor, ttl_seconds=3600)` 获得 `AuthenticationSession(token, expires_at)`；退出时调用 `revoke(token)`。不提供匿名发行 token 的 HTTP 路由。登录凭据验证、SSO/OIDC 接入由认证入口负责，不能把调用者自报的 Actor 直接用于发行会话。

该适配器允许缓存失效：Redis 数据丢失会使用户重新登录。认证存储不可用会拒绝请求；不会用进程内旧身份继续授权。文件认证是启动时加载的静态身份配置，修改文件需重启应用。

## 验证范围

`rag_testing/authentication_contracts.py` 使用合成 TTL 存储与真实 ASGI 请求，覆盖过期、撤销、错误数据、不可用返回 503、缺失返回 401、无身份文件启动、插件关闭与错误协议清理。统一入口为 `uv run --no-sync python scripts/check_project.py`。

真实 Redis 验证接入已有 `storage_contract_integration` 套件的 `test_authentication_sessions_on_real_redis`，检查服务端 TTL 删除、重新创建认证对象后读取会话、撤销以及资源清理。它需要专用 loopback Redis，不属于默认离线门禁，也不代表外部身份提供商的集成验收。
