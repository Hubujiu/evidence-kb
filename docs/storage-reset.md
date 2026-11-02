# 显式创建空业务数据集

需要清空知识库业务状态，或在更换存储提供商后从空数据开始时，执行 `kb_platform.storage_reset`。此操作为知识库、索引、原文、会话、上下文和缓存生成新的空间身份，初始化新的持久数据集，并写出一份完整的新存储配置。它不读取、复制或升级历史业务记录。

旧配置、旧数据和共享数据库都不会被删除。应用改用新配置后看到空业务数据。独立费用与物理请求账本的绑定和数据身份保持不变，UNKNOWN 请求和未知预留不会清零。嵌在旧知识库数据集中的 Embedding 作业记录仍保留在旧数据集中，可用旧配置核查；它们不会迁移到新业务数据集，也不会被此命令重放。此操作不会因为启动失败、身份不匹配或提供商不可用而自动执行。

## 执行命令

先停止使用旧配置的应用及后台任务。输入必须是当前 v2 格式的已初始化配置：每个持久绑定已有 `dataset_id`，每个临时缓存绑定已有 `cache_id`。未初始化的示例模板不能直接作为重置输入。

```bash
uv run --no-sync python -m kb_platform.storage_reset \
  --input .local/storage-configured.json \
  --output .local/storage-fresh.json
```

输出文件必须尚不存在，父目录必须存在。命令成功后输出消费者列表；不会打印连接配置或凭据。检查新配置后，将 `KB_STORAGE_CONFIG` 改为该文件，再启动应用并重新导入知识库。

```bash
export KB_STORAGE_CONFIG="$PWD/.local/storage-fresh.json"
```

更换提供商时，先复制已初始化配置到一个新文件，只修改业务实例和服务指向，保留业务绑定的旧 `dataset_id` 或 `cache_id` 作为重置输入；命令会为这些绑定重新创建身份。若业务和账本共用同一实例，应为业务创建新的实例和服务条目，再修改业务绑定的 `service`，不能改变账本使用的实例。仅切换某个业务提供商时，相关知识库、会话、上下文和缓存也一起重置，避免新旧业务状态混用。

## 消费者角色

默认按当前消费者实例名处理，独立于插件实现和存储提供商：

| 消费者实例名 | 角色 | 操作 |
| --- | --- | --- |
| `repository`、`search`、`originals`、`knowledge`、`retrieval` | `knowledge` | 新空数据集 |
| `store`、`sessions` | `sessions` | 新空数据集 |
| `context` | `context` | 新空数据集 |
| `cache` | `cache` | 新持久缓存索引、新临时缓存空间 |
| `provider`、`model-budget`、`models.budget` | `ledger` | 保留 |
| `identity`、`access` | `identity` | 保留 |

实例名称是 `bindings` 的键，不是提供商目录键，例如 `provider` 表示当前模型服务消费者的预算存储绑定。未识别的实例名会使命令失败，不会猜测它的数据能否清空。

第三方消费者或重命名的实例需使用 `--roles roles.json` 提供完整角色映射，键必须与输入配置的 `bindings` 完全一致。例如，一个包含当前知识库、会话、预算及自定义缓存的应用可使用：

```json
{
  "repository": "knowledge",
  "search": "knowledge",
  "originals": "knowledge",
  "store": "sessions",
  "provider": "ledger",
  "answer-cache": "cache"
}
```

```bash
uv run --no-sync python -m kb_platform.storage_reset \
  --input .local/storage-configured.json \
  --output .local/storage-fresh.json \
  --roles roles.json
```

允许的业务角色为 `knowledge`、`sessions`、`context`、`cache`；保留角色为 `ledger`、`identity`、`external`。自定义物理请求账本必须标为 `ledger`，不可放在会随业务重置的空间中。已知账本消费者不能改为业务角色；已知业务消费者不能标为保留角色来跳过关联状态重置。共享同一业务空间的多个绑定在输出中仍共享同一个新空间和数据身份。

## 校验与失败行为

创建提供商之前，命令完整校验配置结构、消费者角色、服务引用、export、绑定身份与空间冲突。提供商自己的连接参数由提供商构造阶段校验，连接与权限由初始化阶段验证。它只初始化业务消费者需要的实例，不打开独立的账本、身份或外部数据实例。临时缓存只更换命名空间，不连接缓存服务或清除任何键。

新配置写入同目录的临时文件并同步落盘，全部初始化完成后才以不覆盖目标的原子操作发布。任何初始化失败都不发布新配置，也不会改写旧配置、删除旧记录或重试历史物理请求。失败可能留下尚未启用的空新空间；这些空间可按本次操作的运维记录单独处理，不能清空共享数据库。输出文件被另一进程抢先创建时，命令失败并保留对方文件。

离线合同位于 [storage_reset_contracts.py](../kb_platform/rag_testing/storage_reset_contracts.py)，使用临时 SQLite 数据、合成 UNKNOWN 记录和缓存替身，验证空业务状态、旧数据及账本保留、创建前拒绝无效输入、失败不发布配置和发布竞争。它不调用模型，也不代表远程数据库已验收。
