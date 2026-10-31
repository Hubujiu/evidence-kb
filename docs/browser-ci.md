# 运行浏览器与数据库验收

CI 的 `integration` 任务在 Linux 上运行 PostgreSQL、完整项目检查和浏览器验收。`required` 同时依赖 quality、secrets 和 integration。结构检查拒绝移除必需任务、忽略退出结果或用固定成功值替代任务结果。

浏览器使用 [tester-army/e2e](https://github.com/tester-army/e2e)，固定 `e2e 0.22.0` 和 `@e2e-dev/web 0.15.4`，依赖由 `e2e/package-lock.json` 锁定。用例使用确定性定位和断言，不配置模型代理。重试和动作缓存关闭。

## 准备环境

安装 Node 22.23.1 和项目锁定的 Python、Rust 依赖。浏览器依赖与 Chromium 安装命令如下。

```sh
npm ci --prefix e2e --ignore-scripts
cd e2e
node node_modules/@e2e-dev/web/dist/bin.js install chromium --with-deps
```

回到项目根目录。启动项目 compose PostgreSQL，为本次验收创建以 `rust_plugin_acceptance_` 开头的独立数据库，并按顺序应用 `sql/*.sql`。应用连接使用 `kb_app`，地址为 `127.0.0.1:55432`。普通项目数据库和远程连接均不能作为验收目标。

## 执行检查

以 PowerShell 为例，为两个命令选择尚不存在的输出目录。

```powershell
$env:KB_INTEGRATION_DSN='postgresql://kb_app:kb_local_app@127.0.0.1:55432/rust_plugin_acceptance_local'
uv run --no-sync python scripts/check_project.py --local-integration --output reports/ci/local-check
uv run --no-sync python -m kb_platform.rag_testing.browser_integration --checker-summary reports/ci/local-check/summary.json --output reports/ci/local-browser
```

第二个命令要求第一步结果为 PASS，使用该轮 `native/` 下的 Rust 宿主及动态库。控制器启动真实应用，通过随机本地端口访问现有工作台，最后请求宿主正常关闭。用例执行真实上传、解析、BM25 索引发布、检索、引用查看、原件下载和租户切换。

## 查看结果

输出包含 `report.json`、宿主日志、浏览器 JSON、JUnit、截图和 trace。控制器拒绝用例缺失、重复、跳过、失败后重试通过、浏览器模型调用或宿主清理失败。CI 在失败时也上传产物，保留 14 天。

这是合成原件的组件验收。数据库与浏览器任务禁用产品远程模型及 Embedding。浏览器报告另外记录并核对框架模型用量。它不替代 `kb_platform/rag_testing` 的统一 RAG 评分，也不代表付费模型答案质量。真实模型验收继续使用显式授权、有限预算及独立输出目录。
