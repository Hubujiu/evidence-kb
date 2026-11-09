# Evidence KB Web

知识库工作台前端。需要 Node.js 22；先构建前端，再按项目根目录 README 启动本地 API，访问 http://127.0.0.1:58080/。

```powershell
cd web
npm ci
npm run build
```

开发时可使用 `npm run dev`，通过 Vite 代理连接本地 API。

打开终端显示的地址，在页面中输入 `.local/identities.json` 中的本地令牌。开发服务器将 API 请求代理到 `http://127.0.0.1:58080`。令牌仅保存在页面内存中。真实问答依赖后端模型配置，可能产生模型调用费用；调用失败或结果不明时，请先恢复会话核对，不要盲目重试。

`npm run build` 生成 `dist/`，由知识库服务提供。请在可信的 HTTPS 或本机环境中使用。

输入栏和等待状态使用 [Beautiful UI Prompt Bar 和 Shimmer](https://www.beautifului.dev/) registry 源码，按项目需求接入受控输入、Lucide 和后端调用，并关闭演示模型、附件与命令。基础按钮和弹窗使用 shadcn/ui 的 Base UI 组件。上游许可证见 `THIRD_PARTY_LICENSES/beautiful-ui.txt`；registry 的 foundation 样式中移除了一个多余的闭合括号。

离线端到端检查（需先运行 `npm ci`，并在 `e2e/` 执行 `npm ci`）：

```powershell
uv run --no-sync python e2e/run_web_chat.py
uv run --no-sync python e2e/run_workbench.py
```

检查使用真实会话 HTTP 路由、会话存储及知识库工作流，数据和模型为合成夹具，不发起付费调用。它验证交互和协议，不代表生产 RAG 质量或真实模型验收。

文档工作台测试使用独立临时存储和 Rust 主宿主，覆盖创建、上传、BM25 发布、检索和原文预览。默认未启用远程模型。
