# 对话流式输出

2026-09-11，旁支单独实现；不执行主任务的功能计划，不改安装器或版本号。

## 已实现

- 会话 worker 建立一个仅当前 Vault / 轮次可读取的内存预览通道；通过 ContextVar 传入既有 asyncio.to_thread 模型调用。
- StepFun 和 Compatible 适配器在有会话通道时发送 `stream: true`，增量解析 Chat Completions SSE。其他模型任务仍用原有非流式调用。
- 只提取顶层 JSON `answer` 的已完整解码部分；不显示 reasoning、原始 JSON、引用或未完成的 Unicode 转义。正文最多 6000 字符，传输总量与事件正文均有限额。
- 模型网关绑定经授权的上下文哈希；读取预览时重新执行会话 authority，防止授权、来源或认识变化后继续展示旧草稿。预览不写数据库、不写日志，不作为历史回答或认识。
- 桌面进程当前使用缓冲式 IPC HTTP 桥，因此前端在活跃轮次每 350ms 拉取最新增量正文，24ms 步进平滑显示；模型到后端是真实 SSE，前端不是独立 SSE 连接。空闲会话保持原有 1800ms 刷新。
- 用户向上阅读时不强制滚动。系统减少动态效果设置开启时直接显示收到的片段，不逐字动画。
- 停止、失败、超时或关闭轮次会清空预览。停止后客户端立即撤下草稿，供应商连接在下一片段到来时停止读取；不保证供应商同步停止计费。
- 模型完整输出通过原有 schema 和引用校验后，正式回答替换草稿，引用再展示。不支持 SSE 但返回完整 JSON 的端点沿用同一次请求结果，不自动重发。

## 验证与构建边界

单元测试覆盖真实增量到达、顶层字段、转义与中文/emoji、截断、无结束事件、兼容返回、跨上下文隔离、取消和授权/认识变化后草稿撤下。实际 Electron + PostgreSQL + 合成 SSE HTTP transport 已验证首段提前出现、逐字增长、最终替换、停止、断线及引用不会提前出现。

独立前端构建在 `.data/streaming-web`，界面截图在 `.data/release-evidence/streaming/stream-in-progress.png`。未覆盖 `apps/desktop/dist`、主任务的 release 目录或 `.data/release-runtime`；现有安装包尚不包含本次修改，下一次主任务统一打包时需一起构建后端和前端。

复核：

```powershell
.venv/Scripts/python.exe -m pytest tests/ai/test_chat_stream.py tests/application/test_chat_stream_preview.py
cd apps/desktop
npm run typecheck
node node_modules/vite/bin/vite.js build --outDir ../../.data/streaming-web
node scripts/test-streaming-desktop.mjs
```

2026-09-11 主任务接续：流式桌面测试默认改用当前 `dist/index.html`，可通过 `MORROW_TEST_WEB` 指定其他构建。统一构建的流式桌面回归已通过；拆信动画版本 `release-letter-animation/win-unpacked` 已重新打包前后端，包含上述流式能力。
