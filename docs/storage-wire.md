# 存储插件的结构化协议

会话和模型账本通过 `storage.service/v1` 的 `relational` 方法调用结构化存储；方法接收配置中的绑定别名和本文的 JSON 请求。知识库数据与认证授权共同使用明确绑定的 [`knowledge.authority.storage/1`](knowledge-authority-storage.md)，该出口固定一个公开权限数据集，直接接收相同的关系请求。绑定决定空间、数据集和允许的 schema。`PortableStructuredClient` 是 Python 侧的便利适配器，SQLAlchemy 对象只存在于调用方进程内。存储插件收到有限表达式树与数据，返回 JSON；不会收到 SQL 字符串、Python 对象、数据库连接或上下文管理器。

`storage_structured_wire.py` 实现请求和生命周期；`storage_expression_wire.py` 实现公开表达式格式。其他语言可以直接生成这些 JSON，不需要使用 Python 适配器。选择的数据库驱动、事务对象和 schema 元数据是存储插件的内部实现。

## Schema 协商

所有请求都有整数 `version: 1`。初始化时先发送 `{"version":1,"op":"schemas"}`，得到 `schemas` 数组。每项包含 `owner`、`version`、`digest`、`appendonly` 和 `tables`。表声明包含列类型、空值约束、主键、作用域列、唯一约束、外键、检查表达式和索引。

`digest` 是存储提供商声明的不透明版本标识。客户端比较公开 schema 的结构、约束、版本和所有者，匹配后把提供商声明的 `digest` 原样用于初始化。比较不依赖 Python、SQLAlchemy 的摘要算法或表与约束的声明顺序；结构或所有者不匹配时拒绝初始化。客户端不能自行推断存储空间或上传任意 DDL。

```json
{"version":1,"op":"prepare","owner":"sessions","schema_version":1,"digest":"<advertised digest>"}
```

成功返回 `{"status":"PREPARED"}`。之后发送 `{"version":1,"op":"seal"}`，返回 `{"status":"SEALED"}`。同一 schema 的重复初始化幂等；不在配置授权列表中的 schema 被拒绝。表结构不匹配时停止启动，不迁移历史数据。

列类型有 `null`、`bigint`、`integer`、`boolean`、`float`、`decimal`、`bytes`、`date`、`text`、`json`、`datetime`、`string` 和 `exact_string`。字符串类型可带 `length`，`exact_string` 必须声明长度。默认值使用下文的表达式格式，不接受 SQL 文本。

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

每个存储空间最多同时打开 64 个事务或守卫句柄。单个事务最多 20,000 条语句，读取限额最多 10,000 行和 8 MiB，事务持续时间最多 300 秒；实际可由请求收紧。单条 JSON 消息小于 4 MiB，表达式嵌套最多 48 层。底层读取限额和消息限额分别校验，超过任何一项都失败；调用方应分页处理大结果。

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

二元操作为 `eq`、`ne`、`lt`、`le`、`gt`、`ge`、`add`、`sub`、`mul`、`div`、`mod`、`in`、`not_in`、`is`、`is_not`。函数为 `count`、`sum`、`min`、`max`、`avg`、`coalesce`、`storage_clock`、`storage_after`。时钟由存储端生成；`storage_after` 的参数是秒数。底层结构化能力还会检查函数参数类型和跨数据库语义。

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

验收测试使用真实 SQLite，经完整 JSON 编解码执行会话生命周期、知识库入库/发布/检索/权限、模型 UNKNOWN 记录、过期回滚和守卫续约，不调用外部模型。
