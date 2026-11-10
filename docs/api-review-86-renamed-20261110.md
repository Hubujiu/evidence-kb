# PR 86 插件实例重命名组合回归

本轮基线为 aa2e2f65699e918e2cf7cd7ba6b4d9ade4b223e3。此前三项业务问题的修复及原始证据保持不变；本轮只修复新增配置来源记录对默认 llm 实例名的依赖。前端无变更，远端模型、Embedding、judge 调用上限均为零。

## 根因与修改

完整 application profile 将实例重命名并更新 bindings 后，注入 model_clients、未注入 models，会在正式装配前访问 plugins['llm'] 并触发 KeyError。原重命名合同使用 models，因条件短路未覆盖这个组合。

来源记录现在包装实际 foundation.llm 工厂，在工厂成功执行后读取其实际 config。使用注入注册表或客户端时报告 explicit；config_path 分支使用文件时保持配置文件来源。没有新增实例名查找、修改公共合同、改变客户端选择或放宽 MODEL_SERVICE_ALREADY_CONFIGURED。

新增合同重命名全部实例并更新 bindings，不传 models，只注入 model_clients，执行真实装配、认证、发布和会话操作，检查来源 explicit。修复前明确复现 KeyError，修复后通过，见[修复前](../.audit/api-review-86-renamed-before.log)与[修复后](../.audit/api-review-86-renamed-after.log)。

现有 HTTP 诊断合同新增反向组合：全部实例改名，config_path 生效，同时传入不同 provider_id 的客户端。检查实际 provider 仍来自文件，来源为 application_profile，忽略环境变量不误报生效。

## 验证范围

11 个相邻合同已通过，最终完整普通入口在 source-01 通过：1781 个离线合同，失败、错误、跳过、预期失败均为 0；48 个基础集成与 28 个宿主集成通过。没有状态机转换改动，不刷新 registry 摘要或模型定义；v6 只追加新增合同身份。普通入口执行 formal lint，远端必需 CI 仍执行完整形式化门禁。本轮不重复运行本地 formal-only，也不引用旧提交的 CI 作为当前验收。

[本轮机器摘要](../.audit/api-review-86-renamed-validation.json)记录实际门禁结果、分母与源码核验。当前非文档源码的文件集合与逐文件哈希均匹配冻结快照。复用24条冻结trace执行当前评分，新增远端模型、Embedding、judge调用均为0。完整原始产物位于本地 reports/api-review-86-rename-20261110/final-01，不提交reports副本。独立同模型复核确认工厂配置来源、优先级与既有冲突守卫保持一致；跨模型复核仍未完成。
