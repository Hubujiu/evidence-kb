# 后端接口交付

本轮不修改前端。接口继续使用原有 Bearer 认证与知识库权限。默认禁止文档进入模型上下文，显式授权后才开放。交付分支为 codex/api-backend-delivery。

## 文档管理

| 接口 | 用途 |
| --- | --- |
| GET /knowledge-bases/{kb_id}/documents | 清单、解析状态、当前发布版本、用途策略和问答阻断原因 |
| POST /knowledge-bases/{kb_id}/sources | 新增，或通过 source_id 明确更新现有来源 |
| PUT /sources/{source_id}/policy | 修改用途策略与允许的 Provider |
| PUT /sources/{source_id}/readers | 修改来源读者 |
| GET /knowledge-bases/{kb_id}/sources/{source_id}/versions | 查看不可变原文版本身份与 SHA-256 |
| POST /knowledge-bases/{kb_id}/sources/{source_id}/reparse | 从已存原件重新解析，沿用来源身份 |
| DELETE /knowledge-bases/{kb_id}/bindings/{binding_id} | 明确移出活跃集合，解除 PARTIAL 的构建阻断 |

清单返回 authority_revision。上传表单及 policy、readers、binding、reparse 请求可以携带 expected_revision；解除绑定通过同名查询参数传递。过期修订返回 409 AUTHORITY_REVISION_CONFLICT。旧客户端未提供条件时保留既有调用约定。

同名文件不会自动覆盖。修订必须显式传 source_id。构建读取候选绑定，检索和 Agent 读取当前 manifest 的不可变版本，并同时复核当前权限。候选修订发布前继续使用旧发布版本。移除与撤权立即影响证据读取。policy 是完整策略对象，修改前读取当前值并保留其他用途标志。

清单的 provider_id 默认来自当前应用实际模型，也可显式选择某个 Provider 进行策略诊断。qa_ready 是该 Provider 对当前已发布来源的可用性判断，不证明任意问题都能得到完整答案。build_blockers 描述候选文档的构建问题。needs_publication 描述尚未发布、候选版本变化或移除绑定，不把单纯问答授权变更误报成必须重新索引。

## 请求查询与公开错误

GET /agent/sessions/{session_id}/requests/{request_id} 查询同一逻辑请求，request_id 中的保留字符须 URL 编码。路由保留斜杠身份。查询不续租、不执行模型、不重发。所有者和当前知识库权限始终复核。

| status | recovery_action | 含义 |
| --- | --- | --- |
| running | wait | 原请求尚未终结，不能推定其未发送 |
| interrupted | inspect_request | 租约已过期，查询不修改原行 |
| not_found | verify_request_id | 核对请求身份，不能据此推定零发送 |
| completed | retrieve_result | 取回保存的业务结果，错误结果仍是错误 |
| reconciliation_required | inspect_operation | 持久 model_failed 记录表明提交结果未知 |

observations 仅包含允许公开的逐调用事实，例如物理身份、操作身份、已观察用量、HTTP 状态及捕获和结算确认。成功调用为 recovery_action=none；缓存结果保留 cache_kind 和 attempts，不能冒充新的物理请求。聚合 send_status 保守为 unknown。撤销证据授权后，完成响应脱敏且不返回 observations。

公开错误采用同一白名单字段。HTTP 保留 Retry-After，未知用量不会补零，原始提示词、捕获目录、认证值和供应商响应体不公开。内部仍可保存更完整的审计记录。

## 模型准入与扩展

通用 HTTP 适配器在响应后先观察与捕获事实，再尝试一次结算。ledger_settlement 与 capture_state 分开表示。生产者 guard 覆盖完整调用；健康重叠请求返回明确 BUDGET_PRODUCER_BUSY 且 model_sent=false，已有未决请求继续保留预留并严格阻断。本轮未开放多请求并发或自动排队。

可信启动装配调用 model_provider_registry.register_provider_adapter 注册协议、能力、options Schema 与工厂。普通模型配置的 adapter 和 options 由注册目录验证。配置不支持任意模块导入。内置 openai-compatible 与 anthropic 保持可用。

GET /diagnostics 返回实际应用图摘要、配置来源类别、当前 Provider、当前入口不读取的环境变量和开发工具是否存在。它不探测供应商、不发送模型请求，也不输出配置内容、密钥或本机配置路径。

配置来源在实际装配时记录。显式 profile 内的文件记为 application_profile，被忽略的 KB_MODELS_CONFIG、KB_PROVIDERS_CONFIG、KB_STORAGE_CONFIG 会列入 inactive_environment。显式模型或存储对象记为 explicit。默认装配采用环境覆盖时才标记对应环境变量，后续修改进程环境不会改写既有实例的诊断。

未决交付实例的只读恢复接口与限制见 [交付恢复说明](delivery-recovery-api.md)。

## 验收

必要组件合同通过统一 checker 发现。contract_http_integration 由原有受控 host-integration 阶段执行，保留同一生产应用装配、真实临时存储、检索、会话与权限，只替换远程模型边界。响应后结算失败、权限撤销、斜杠请求身份和缓存用量分别有回归。

这些合成合同验证接口与故障边界。统一评分器控制继续复用冻结 trace，不是业务召回或答案质量成绩。本轮新增远端模型、Embedding 与 judge 调用均为零。最终门禁状态见[验收记录](api-delivery-validation-20261110.md)。
