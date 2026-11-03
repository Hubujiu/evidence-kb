# 存储插件的结构化协议

会话和模型账本通过 `storage.service/v1` 的 `relational` 方法调用结构化存储；方法接收配置中的绑定别名和本文的 JSON 请求。知识库数据与认证授权共同使用明确绑定的 [`knowledge.authority.storage/1`](knowledge-authority-storage.md)，该出口固定一个公开权限数据集，直接接收相同的关系请求。绑定决定空间、数据集和允许的 schema。`PortableStructuredClient` 是 Python 侧的便利适配器，SQLAlchemy 对象只存在于调用方进程内。存储插件收到有限表达式树与数据，返回 JSON；不会收到 SQL 字符串、Python 对象、数据库连接或上下文管理器。

`storage_structured_wire.py` 实现请求和生命周期；`storage_expression_wire.py` 实现公开表达式格式；`storage_json_wire.py` 对大请求和大结果执行有界 JSON 分片。其他语言可以直接生成这些 JSON，不需要使用 Python 适配器。选择的数据库驱动、事务对象和 schema 元数据是存储插件的内部实现。

## Schema 协商

所有请求都有整数 `version: 1`。初始化时先发送 `{"version":1,"op":"schemas"}`，得到 `schemas` 数组。每项包含 `owner`、`version`、`digest`、`appendonly` 和 `tables`。表声明包含列类型、空值约束、主键、作用域列、唯一约束、外键、检查表达式和索引。

`digest` 是存储提供商声明的不透明版本标识。客户端比较公开 schema 的结构、约束、版本和所有者，匹配后把提供商声明的 `digest` 原样用于初始化。比较不依赖 Python、SQLAlchemy 的摘要算法或表与约束的声明顺序；结构或所有者不匹配时拒绝初始化。客户端不能自行推断存储空间或上传任意 DDL。

```json
{"version":1,"op":"prepare","owner":"sessions","schema_version":1,"digest":"<advertised digest>"}
```

成功返回 `{"status":"PREPARED"}`。之后发送 `{"version":1,"op":"seal"}`，返回 `{"status":"SEALED"}`。同一 schema 的重复初始化幂等；已成功准备的身份固定为 `owner`、`version` 和提供商 `digest`，不随配置对象后续变化而改变。不在配置授权列表中的 schema 被拒绝。客户端只把精确的 `PREPARED` / `SEALED` 响应当作相应操作成功；丢失或无效响应不证明服务端未执行，也不会触发自动重发。表结构不匹配时停止启动，不迁移历史数据。`prepare` 完成后修改已注册元数据不是受支持的在线 schema 更新；需要显式迁移/新的初始化流程。原 `Table` 身份保持不变。

列类型有 `null`、`bigint`、`integer`、`boolean`、`float`、`decimal`、`bytes`、`date`、`text`、`json`、`datetime`、`string` 和 `exact_string`。字符串类型可带 `length`，`exact_string` 必须声明长度。`decimal` 必须携带 `precision`、`scale`、`as_decimal` 和 `decimal_return_scale`：未指定的精度、小数位和返回位数为 `null`，`as_decimal` 为布尔值。指定的精度在 1–1000 之间，小数位和返回位数在 0–1000 之间，小数位不能超过已指定的精度；具体后端仍可拒绝自身不支持的组合。这些字段参与 schema 比较和字面值恢复，`decimal(12,2)` 不会与 `decimal(6,4)` 协商成同一结构。默认值使用下文的表达式格式，不接受 SQL 文本。

## 事务请求

```json
{
  "version":1,
  "op":"begin",
  "write":true,
  "scope":{"tenant_id":"tenant-a","kb_id":"kb-a"},
  "limits":{"max_rows":10000,"max_bytes":8388608,"max_statements":20000,"max_seconds":30},
  "operation_id":"caller-generated-operation-id"
}
```

响应包含随机 `handle` 和原样返回的 `operation_id`。句柄绑定到该存储服务实例中的事务，不能用于其他存储绑定；其他调用方不能通过句柄指定数据集或存储空间。消费者的业务鉴权负责提供可信的 `scope`，存储适配器负责对 schema 中声明的作用域列强制过滤。缺少作用域的读取不会读取其他租户的数据，替换作用域的写入被拒绝。

`execute` 请求包含 `handle` 和 `statement`；`insert` 请求包含 `handle`、`table`、`values`、`conflict_keys`、`updates`、`returning`。`updates` 为 `null` 表示遇到声明的唯一键冲突时不更新。写入、更新和删除继续遵守主键不可变、只追加表和外键规则。

成功返回 `{"rows":[...],"rowcount":N}`。所有行的字段使用下文的带类型值；`rowcount`、限额和协议版本等控制字段使用 JSON 整数。

```json
{"version":1,"op":"finish","handle":"<handle>","commit":true}
```

提交成功返回 `{"status":"COMMITTED","operation_id":"..."}`；`commit:false` 返回 `ROLLED_BACK`。表达式执行失败、结果无法编码、结果超限、超时或宿主关闭都会回滚仍然打开的事务。读取成功不代表事务已经提交。客户端必须检查 `finish` 的结果。

`operation_id` 用于识别提交结果不明的操作，**不是自动重试或幂等写入承诺**。提交阶段连接丢失时保留 `COMMIT_UNKNOWN` 及原始 `operation_id`，不自动重放写入。服务保存最近 256 个结束句柄的结果，重复 `finish` 返回已知结果；服务重启或结果被逐出后不可据此推断提交状态。

每个存储空间最多同时打开 64 个事务或守卫句柄。单个事务最多 20,000 条语句，读取限额最多 10,000 行和 8 MiB，事务持续时间最多 300 秒；实际可由请求收紧。单条 JSON 消息小于 4 MiB，表达式嵌套最多 48 层、最多 1,000,000 个 JSON 节点。大逻辑请求和结果使用下文分片，底层读取预算保持不变；调用方仍应分页处理超过事务读取预算的结果。

## 大 JSON 请求与结果

解析文档可能从 100 KB 原文件展开为超过 4 MiB 的结构化记录，因此原件分块不能替代关系请求和结果的分片。传输层只暂存有限 JSON，仍通过原来的有限表达式解释器和数据库事务执行；所有实际跨进程或公共服务帧继续受到 4 MiB 限制。

每个绑定对应的传输端点最多保留 16 个传输句柄，单个逻辑 JSON 最多 64 MiB，所有暂存声明大小合计最多 256 MiB。超过 1 MiB 的暂存文件落到临时文件；传输空闲 300 秒后在后续调用中清理，端点关闭时立即清理。单块为最多 256 KiB 原始 UTF-8 JSON 字节，使用标准 Base64 编码，偏移按原始字节计数。JSON 总大小、节点数和嵌套深度独立校验，分片不能绕过这些限制。

大 `execute` 或 `insert` 请求按以下顺序发送：

1. 将包含 `version:1` 的完整逻辑请求编码为 UTF-8 JSON，计算字节数和 SHA-256。
2. 发送 `{"version":1,"op":"wire_begin","transaction":"<transaction handle>","bytes":N,"sha256":"<digest>"}`。服务先验证事务仍然有效，再返回 `{"handle":"<transfer handle>","bytes":N,"sha256":"<digest>"}`。
3. 逐块发送 `{"version":1,"op":"wire_write","handle":"<transfer handle>","offset":0,"data_base64":"..."}`，响应的 `offset` 必须等于已接收的字节数。偏移必须严格连续。
4. 发送 `{"version":1,"op":"wire_dispatch","handle":"<transfer handle>"}`。服务检查完整长度和哈希，只接受绑定到同一事务的 `execute` 或 `insert`，消费传输句柄并执行一次。不能把另一个传输操作、提交或任意 SQL 嵌入其中。

大结果以 `{"wire_result":{"handle":"<transfer handle>","bytes":N,"sha256":"<digest>"}}` 返回。客户端发送 `{"version":1,"op":"wire_read","handle":"<transfer handle>","offset":0}` 顺序读取，响应包含 `data_base64`、下一个 `offset` 和布尔值 `done`。接收完整长度后校验 SHA-256，再解析 JSON。最后一块读取成功即释放该传输句柄。

取消使用 `{"version":1,"op":"wire_cancel","handle":"<transfer handle>"}`，响应为 `{"closed":true}` 或 `{"closed":false}`。Python 客户端在上传、结果读取或校验失败时执行取消，不自动重发 `wire_dispatch` 或数据写入。已进入执行的分发不会因另一个取消请求重复执行；其暂存文件在该次分发结束时释放。

关系请求的上传和结果句柄均绑定到原事务，必须在结束事务前消费结果。事务提交、回滚或超时会释放尚未消费的传输文件；因此结果描述响应丢失后，客户端的回滚仍能清理自己未获得句柄的结果。大结果的编码与暂存容量检查发生在事务工作线程内，检查失败会回滚事务。`finish` 仍使用小消息，提交结果不明时仍返回原 `operation_id` 的 `COMMIT_UNKNOWN`，不会通过分片自动重试提交。

通用存储 `invoke` 的公共请求为 `{"binding":"search.bm25","request":{...}}`。内部逻辑请求为 `{"version":1,"op":"invoke","method":"stage","args":[...],"kwargs":{...}}`，参数和结果沿用存储值编码。它复用相同分片格式，由每个绑定的端点显式声明仅允许 `invoke`，其 `transaction` 必须为 `null`，句柄不能跨绑定使用。`stage` 的整批索引记录、`read_records` 和 `get_many` 的整批结果均可分片；所有参数共享 250,000 个领域值节点的有限编码预算，编码后的 JSON 仍受上述大小、深度和节点数限制。既有低层进程适配器的默认 20,000 节点限制保持不变。

通用存储调用不获得关系事务的提交或回滚保证；具体存储方法保持自身的原子性语义，传输失败不能解释为可以自动重放写入。客户端不会在响应丢失后重发分发请求。独立存储进程验收同时覆盖大批量索引写入、读取、文档批读、每帧大小和子进程清理。

## 有限表达式树

表达式的判别字段是 `node`。只接受下列节点和声明的字段，未知节点不会作为 SQL 执行。

| 节点 | 其他字段 | 含义 |
| --- | --- | --- |
| `value` | `value`、可选 `type` | 带类型字面值 |
| `column` | `table`、`name` | schema 中声明的列 |
| `list` | `items` | `in`/`not_in` 的值列表 |
| `label` | `name`、`expression` | 结果列名称 |
| `binary` | `op`、`left`、`right` | 比较或算术 |
| `boolean` | `op`、`items` | `and` 或 `or` |
| `unary` | `op`、`expression` | `asc`、`desc`、`not`、`neg`、`is_true`、`is_false` |
| `case` | `when`、`else` | 条件/结果二元组列表和默认表达式 |
| `function` | `name`、`args` | 允许的聚合函数或存储时钟 |

二元操作为 `eq`、`ne`、`lt`、`le`、`gt`、`ge`、`add`、`sub`、`mul`、`div`、`mod`、`in`、`not_in`、`is`、`is_not`。函数为 `count`、`sum`、`min`、`max`、`avg`、`coalesce`、`storage_clock`、`storage_after`。`{"node":"function","name":"count","args":[]}` 表示 `COUNT(*)`，统计行数；传入列参数的 `count` 继续忽略该列的空值。时钟由存储端生成；`storage_after` 的参数是秒数。底层结构化能力还会检查函数参数类型和跨数据库语义。

选择语句包含 `kind:"select"`、`columns`、`from`、`where`、`order`、`limit`、`offset`、`lock`。`columns`、`where` 和 `order` 是表达式数组；`limit` 和 `offset` 是整数字面值表达式或 `null`；`lock` 是布尔值。`from` 使用 `table`、`alias` 或 `join` 节点，连接显式声明左右来源、`on` 条件以及 `outer`/`full` 布尔值。

更新语句包含 `kind:"update"`、`table`、`values`、`where`、`returning`；删除语句包含 `kind:"delete"`、`table`、`where`、`returning`。`values` 是列名到表达式的映射。协议不会通过 SQL 文本补充未支持的查询能力；不支持的 SQLAlchemy 表达式在客户端被拒绝。

```json
{
  "version":1,"op":"execute","handle":"<handle>",
  "statement":{
    "kind":"select",
    "columns":[{"node":"column","table":"sessions","name":"id"}],
    "from":[{"node":"table","name":"sessions"}],
    "where":[{"node":"binary","op":"eq","left":{"node":"column","table":"sessions","name":"tenant"},"right":{"node":"value","value":"tenant-a"}}],
    "order":[],"limit":{"node":"value","value":{"type":"integer","value":"50"}},
    "offset":null,"lock":false
  }
}
```

## 数据值与守卫

字符串、布尔值、`null` 直接使用 JSON。其他值采用 `{"type":"...","value":...}`：整数、浮点数和十进制数的值为十进制字符串；字节为标准 Base64；日期为 ISO 日期；时间为带时区 ISO 时间且统一为 UTC；JSON 容器为 `type:"json"`，`value` 保留对象或数组。非有限数、无时区时间和任意 Python 对象被拒绝。大整数使用字符串编码，避免其他语言的浮点精度损失。

`{"version":1,"op":"guard","name":"budget-producer","nowait":true}` 返回独立守卫句柄。`ping` 续约其 60 秒租期；`finish` 释放守卫并返回 `RELEASED`。Python 客户端自动每 20 秒续约。守卫独立于事务，避免把模型网络请求包进数据库事务；竞争失败仍为 `DATABASE_GUARD_BUSY`。宿主停止或客户端租期失效后释放守卫。账本中的 `STARTED`/`UNKNOWN` 记录继续阻止不安全的模型重试。

验收测试使用真实 SQLite，经完整 JSON 编解码执行会话生命周期、知识库入库/发布/检索/权限、模型 UNKNOWN 记录、过期回滚和守卫续约。大文档合同覆盖 100 KB 文本展开后的 4 MiB 以上解析记录，通过真实 authority 公共服务写入和读回，并验证租户隔离、最大实际帧大小、上传/下载中断、分发响应丢失、容量失败回滚、提交 UNKNOWN 和临时文件释放。独立存储进程验收同时写入和读回 4 MiB 以上会话产物。测试不调用外部模型。
