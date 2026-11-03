# 官方 SANY 同行 Boolean RHS 审阅控制

这些是门禁控制输入，不是额外登记的产品状态机，也不计入模型覆盖。四份历史 `.tla.txt` 是下列 Git 对象的完整、未改写原文；后缀避免把历史坏模型当作活动 TLC 模型。`manifest.json` 固定来源、源 SHA-256 和完整官方 XML SHA-256。

| 控制 | 来源 | 指定结果 |
|---|---|---|
| postflight-bad | `aa909eef335cc95f75cff3fc5e76e6f87b723b51:formal/session_authority/PostflightAuthority.tla` | L83 `commitAllowed`、L92 `evidenceChecked` 两项审阅错误 |
| postflight-good | `e716efcf4544e994888dce6eea22f152e2e95455:formal/session_authority/PostflightAuthority.tla` | 零项 |
| projection-bad | `2e03546346b05b5f95a4c2f3f07672db1075cf54:formal/cache_evidence/ErrorProjection.tla` | L31 `privateVisible` 一项审阅错误 |
| projection-good | `21af9c5232816dbd34af60dd62baa8efb2167d89:formal/cache_evidence/ErrorProjection.tla` | 零项 |
| safe-shapes | 合成语法控制 | 相邻赋值、UNCHANGED、完整括号 RHS、IF/CASE/LET 条件、独立行 guard 均零项 |
| intentional-guard | 合成语法控制 | 即使作者有意写同行 guard，也必须明确审阅，不自动修改语义 |
| scope-limits | 合成边界控制 | 不宣称识别跨行意图、析取或 conjunction 链末尾 |

历史 PR35 的两个式子原本被官方 SANY 解析为“primed equality 加独立 guard”，会隐藏拒绝 replay 或 evidence 撤销后的 Commit；PR44 的同行表达式会隐藏 coded projection。修正版仅为整个 Boolean RHS 加括号并补充领域观察。这里的诊断不是新增 production bug，更不表示消除了 PR35 已知 authority→commit 跨存储限制。

## 导出与复核

优先复用固定官方工具提供的语义树，不实现 TLA 文本 parser。工具身份沿用 `formal/toolchain-v1.json` 的 [官方 v1.7.4](https://github.com/tlaplus/tlaplus/releases/tag/v1.7.4)，SHA-256：

`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`

对每份源原文，将其按声明名复制为独立临时目录中的 `Module.tla`，再执行：

```text
java -Xmx256m -XX:MaxMetaspaceSize=128m -Djava.io.tmpdir=<owned-temp> -cp <verified-jar> tla2sany.xml.XMLExporter -o <Module.tla>
```

stdout 原始 XML 字节作为明文 `.xml` 保存，未裁剪、重新序列化、压缩或改写；stderr 单独留在运行证据。`-o` 为这个固定版本的离线导出选项，不使用默认的外部 XSD 获取。七份 XML 合计 841,014 bytes，七份源原文合计 24,270 bytes。本目录 `.gitattributes` 保持 LF，避免 Windows checkout 破坏精确字节摘要。

ordinary sealed contracts 只读取、校验并解析这些 XML，绝不启动 Java、下载 jar 或调用网络。独立 formal 阶段从源原文重新调用已校验的 pinned exporter，并要求精确的行号/赋值名/guard 名称；非零退出、超时、缺失/损坏 XML、缺失 UID 或不完整控制集都是错误，不能满足负控。

## 窄规则与边界

只检查官方 `OpApplNode` 中直接二元 `\land` 的左项为 `=(prime(...), RHS)`、右项既非另一 primed equality 也非 `UNCHANGED`，且左项结束行等于右项开始行的形态。模块、operator UID 和位置都来自官方 XML；不跨 IF/CASE/LET 边界，也不自行展开 conjunction 链或重建 TLA 语法。

合法但模糊的 `x' = next /\ Guard(next)` 仍会提示 `INLINE_PRIMED_BOOLEAN_REVIEW`。若本意是赋布尔合取值，写完整 RHS 括号；若本意是独立 guard，把 guard 放到下一行 conjunct。**不能全局自动补 RHS 括号**。

零项不代表没有错误：跨行、helper、record/EXCEPT、`\/`/`=>`/`<=>`、链式后段、过强 guard、遗漏 action、cfg/bounds 屏蔽的分支以及源码精化均不在此规则内。`scope-limits` 明确保留这些阴性边界，避免给直接二元实现贴上完整逻辑解析的标签。

每个领域仍需人工定义 terminal/action-after witnesses。仅在 Init 中取到某个 kind/outcome 不能证明该分支执行；状态相同的不同动作原因也不能随意互换。这个 gate 不代替 safety、命名负控、终态 reachability、实际代码回归或独立审查。
