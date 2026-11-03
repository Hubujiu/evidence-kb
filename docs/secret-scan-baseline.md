# 历史源码哈希的密钥扫描误报

`.gitleaksignore` 仅登记提交 `6efd45e2129b0c0a2afa7a6d51fa60a561f69bc4` 中 `formal/combination-inputs-v1.json` 的 11 个精确指纹。它们是版本化源码 blob 的 SHA-256，不是访问凭证。

逐条使用 `git show <commit>:<source path>` 读取原始 blob，再计算 SHA-256，结果均与清单值一致。

| 清单行号 | 对应源码 blob |
| --- | --- |
| 3904 | `formal/storage_grants/StorageGrantsEntryNegative.cfg` |
| 3917 | `formal/auth_session/AuthSession.tla` |
| 3918 | `formal/auth_session/AuthSession.cfg` |
| 3919 | `formal/auth_session/AuthSessionNegative.cfg` |
| 3981 | `formal/cache_evidence/StageCacheKeyNegative.cfg` |
| 4052 | `formal/session_catalog/SessionCatalogSessionKeyBypass.cfg` |
| 4081 | `formal/remote_retry/GoogleRetryAuth.tla` |
| 4082 | `formal/remote_retry/GoogleRetryAuth.cfg` |
| 4091 | `formal/remote_retry/SeededAuthBudgetReset.cfg` |
| 4092 | `formal/remote_retry/SeededRepeatedAuthentication.cfg` |
| 4093 | `formal/remote_retry/SeededOAuthModelCount.cfg` |

Gitleaks v8.30.1 对全部可达历史的扫描复现了这 11 条误报。加入精确指纹后同范围扫描通过。使用同一指纹文件扫描新生成的合成 api_key，仍以退出码 1 检出。未修改扫描规则、目录范围或历史。

新提交、路径、行号或规则产生的其他发现仍会阻止合并。不得将此清单扩大为整目录、规则或 SHA-256 模式豁免。
