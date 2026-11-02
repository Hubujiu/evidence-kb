# 有限状态模型与源码映射门禁

`formal/registry-v1.json` 是版本化的状态机盘点和审查映射，不是全系统正确性证明。每个条目记录源码所有者、状态、转换、不变量、假设、排除范围和源码锚点。初始盘点覆盖 25 个状态机家族；候选发现不是穷尽性分析。`unmodeled` 表示只有盘点，`partial` 表示已有有限抽象模型且必须列出 `remaining_obligations`。没有 `complete` 状态：一个模型通过不能证明整个家族或具体实现无缺陷。

## 两个必需本地入口

```sh
uv sync --locked --group dev
uv run --no-sync python scripts/check_project.py
uv run --no-sync python scripts/install_formal_tools.py
uv run --no-sync python scripts/check_project.py --formal-only
```

TLC 需要 Java 21。安装脚本是显式联网准备步骤，检查过程中不会安装、下载、调用提供者或付费模型。也可设置 `TLA_TOOLS_JAR` 指向已有的已核验文件；内容仍须符合锁定的大小和 SHA-256。缺失 Java/jar、版本不符或校验失败均失败，不跳过。

默认 checker 保留全部原有 Rust、组件合同、进程集成、lint 和评分控制门禁，并额外检查状态盘点及源码映射。`--formal-only` 是单独的附加门禁，不执行也不替代普通质量门禁。CI 的独立 `formal` job 避免在四个平台/Python 组合及数据库集成中重复 TLC；`required` 同时要求原有 quality/secrets/integration 和 formal 全部成功，取消、跳过或失败均不能通过。

## 固定工具与成熟模式

`formal/toolchain-v1.json` 固定 [官方 TLA+ v1.7.4](https://github.com/tlaplus/tlaplus/releases/tag/v1.7.4) 的 `tla2tools.jar`；其 TLC 为 2.19（2024-08-08），不是可变 prerelease。固定源提交为 `5a47802b5c391f59ecdd44117981f4ff8c0656ba`，SHA-256 为 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`。该发行版包含 issue 971 的活性检查健全性修复。

建模采用显式 Init/Next、类型和安全不变量、有限域、小界限先行、仅在明确公平性假设下检查活性的常见 TLA+ 方法。模型必须对应实际所有者和故障边界；借鉴成熟实现的理由及源版本写入各模型 README，不以重新实现运行时来“满足模型”。TLA+ 及 TLC 的 [官方文档](https://lamport.azurewebsites.net/tla/tla.html) 和 [官方示例](https://github.com/tlaplus/Examples) 是方法参考，不是本项目精化证明。

## 源码 lint 的真实保证

- Python 锚点使用去除位置元数据且保留空列表字段的 `ast.dump` SHA-256；Python 3.13 显式设置 `show_empty=True`，与 3.12 的格式保持一致，由跨版本固定摘要合同验证。空白变化不触发，已登记符号的状态、守卫或转换修改会触发审查。
- Rust 暂以完整源文件 SHA-256 加声明存在性检查保守绑定，仅按 [Rust 输入规则](https://doc.rust-lang.org/reference/input-format.html#crlf-normalization)将 CRLF 对规范为 LF，避免 Windows checkout 产生假漂移；单独 CR 和其他内容不被抹除。非语义修改仍可能要求重新审查，不冒充 Rust AST 或精化验证。
- 发现器检查 Python 对 `state/status/phase` 的赋值、`State/Status/Phase` 后缀类，以及 Rust 同后缀枚举。未登记候选失败。HTTP 状态、纯观测 DTO 等假阳性须逐符号说明理由；陈旧排除也失败。合同测试文件和 Rust tests 文件不是生产候选扫描对象，但可显式绑定回归符号。
- 发现器不能识别全部隐式状态、别名写入、动态代码或跨过程转换；既有显式绑定补充发现器。更新哈希只表示重新审查了映射，不能证明源码等价于模型。
- 新 `.tla/.cfg` 必须登记；每个正例必须有非空、唯一的不变量名称列表，活性属性列表可为空，二者须与配置一致。删除配置中的全部断言并同步清空登记不能通过。有限界限须显式登记；界限说明本身不是机器可验证的有界性证明，TLC 的时间和内存限制另行强制执行。

新增或修改转换后，先审查相关模型/假设/排除和回归，再更新对应源码摘要；不要机械重算所有摘要消除失败。模型或抽象本身可能遗漏行为，代码审查、负控和实际回归仍然必需。

## 实际检查与反例证据

每次 TLC 运行使用 1 worker、固定 seed/fingerprint、最大 512 MiB Java heap、128 MiB metaspace 和最长 120 秒的单模型超时；统一 formal 阶段另有 900 秒限制。界限内真实穷举，不使用随机模拟、旧 PASS 缓存或预设通过标记。检查保留每次输出、命令、Java 实际版本、工具锁、registry 摘要、输入模型副本及摘要、状态统计和退出码。

通过判定解析固定版本的 [TLC 帧事件与退出码](https://github.com/tlaplus/tlaplus/blob/5a47802b5c391f59ecdd44117981f4ff8c0656ba/tlatools/org.lamport.tlatools/src/tlc2/output/EC.java)，而不是数 PASS 文本：正例要求 exit 0、SUCCESS 事件、非零状态、队列耗尽、无错误且日志帧完整。负例要求 safety exit 12 和指定不变量的违反事件；解析失败、无关错误或超时不是有效反例。

`formal/controls` 有真实执行的合成正确模型、注入提前释放的错误变体，以及故意无效的配置。它们只检查工具链灵敏度，不计入产品覆盖。每个实际模型也必须带有对应命名负控。`kb_platform/rag_testing/formal_state_machine_contracts.py` 同时提供可由统一 unittest 发现的状态/守卫变异 lint 负控、TLC 输出判定合同，以及实际运行证据的附加合同；普通离线 worker 的子进程禁令不变，Java 只由独立受控阶段启动。

### 官方 SANY 同行布尔赋值审阅

独立 formal required 阶段在 TLC 前，使用同一已校验 jar 的 `tla2sany.xml.XMLExporter -o` 离线导出每个唯一登记模块的官方语义树。每次导出最多 30 秒、256 MiB heap / 128 MiB metaspace，不放宽既有 TLC 或整个 formal 阶段限制。导出命令、源副本/摘要、原始 XML/摘要、stderr 和真实退出码保留在 `inline-boolean-review/`；解析错误或审阅错误不能被当成零项后继续宣称 TLC 已执行。

`INLINE_PRIMED_BOOLEAN_REVIEW` 仅提示直接二元、同一行的 `x' = a /\ b` 形态：官方 AST 中 equality 的 RHS 已在 `a` 结束，`b` 是独立 guard。另一 primed equality、`UNCHANGED` 和赋值内部 IF/CASE/LET 条件不会被混同；完整 `(a /\ b)` RHS 不报。它要求明确审阅，不推断作者意图、不自动修复：赋值值需完整括号，独立 guard 可移到独立 conjunct 行。

此规则不覆盖跨行意图、链式 conjunction 后段、其他逻辑符、helper、record/EXCEPT、漏 action 或 cfg/bounds 引起的不可达，更不是实现精化证明。手工定义的 terminal/action-after witness 仍必需；仅 Init 取值不算执行证据。

[控制输入说明](../formal/controls/inline_boolean/README.md) 保留 PR35/44 真实旧坏/新好源文件，以及固定官方 exporter 的小型明文 XML fixtures 和来源/hash。ordinary sealed contracts 只读取这些 fixtures，不启动 Java；formal 阶段重新导出并核对指定行/赋值名/guard，不接受“任意错误即负控通过”。工具升级或 exporter 格式变化必须重新审查 fixtures，缺失/坏 XML、未解析 UID、超时和非零退出均明确失败。控制文件不计入产品状态机覆盖。

另有实际活性正负控：活性负控必须在配置中只启用指定 `expected_property`，并同时得到 exit 13、temporal-violation 事件 2116 和完整活性检查事件；只出现同为 exit 13 的 action-property-evaluation 错误不能通过。TLC 不在该违反事件中命名属性，因此用单属性配置及输入摘要绑定反例，不能凭退出码推断失败性质。

默认输出是新的 `.local/checks/<唯一编号>/`；CI 保存完整 formal 输出 14 天。现有结果目录拒绝覆盖。失败、缺失运行或零真实模型不接受；合成控制不能代替产品模型。

有限模型未覆盖的输入规模、操作系统、数据库隔离、持久化、网络交付、时钟、第三方插件和代码精化义务必须保留。特别是“授权在检查时有效”“提交时有效”“响应交付时有效”是不同的安全性质，不得相互替代。
