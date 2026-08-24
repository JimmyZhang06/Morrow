# Vistora Desktop

Vistora 的 Windows 桌面客户端。技术栈为 Electron、React、TypeScript 和 Vite。

当前交付版本为 v0.4：基于真实 EXE 截图推翻了厚重侧栏和嵌套证据卡，改成更轻的桌面导航与编辑批注式证据栏。视觉重置说明见 `../../plan/14-desktop-visual-reset-v0.4.md`。

## 已实现

- Windows 无边框桌面窗口与自定义窗口控制；
- 以记录输入为首要动作的 Today 工作区、全局搜索与快捷键；
- 记录列表、详情、ETag 修订、删除影响说明、本地草稿与离线待同步；
- Memory Inbox、权威详情、证据锚点、确认、纠正、驳回与暂缓；
- 明确标为“非真实分析”的认识界面示例，不用虚构洞察冒充用户数据；
- 行动候选、接受、完成和撤销；在 Action HTTP API 暴露前清楚标注为本机状态；
- 隐私显示偏好与后端连接设置；
- 顶部一键隐私遮罩、API 令牌显示控制与更清晰的服务状态；
- 通过 Electron 主进程安全桥接 FastAPI，不向渲染进程暴露 Node.js；
- 检查 `GET /health/live`、`GET /health/ready` 与 `GET /health/capabilities`；
- 按现有合约对接 Source CRUD、Memory Review 与 Verdict API，接口未挂载时明确降级。

产品评审、信息架构和验收标准见 `../../plan/12-desktop-product-review-v0.2.md`。

## 本地开发

```powershell
cd apps\desktop
npm install
npm run electron:install
npm run dev
```

后端默认地址是 `http://127.0.0.1:8000`，也可在“设置与隐私”中修改。访问令牌只保留在当前应用进程中，API 地址保存在本机。

## 构建 Windows EXE

```powershell
cd apps\desktop
npm run dist:win
```

输出在 `release\Vistora-<version>-x64.exe`。`dist:installer` 可生成安装版；当前交付优先使用无需安装的 portable 版本。

若 GitHub 下载较慢，可在当前 PowerShell 会话为 Electron 官方下载器配置镜像；下载内容仍会由 Electron 包内置的官方 SHA-256 校验：

```powershell
$env:ELECTRON_MIRROR='https://npmmirror.com/mirrors/electron/'
npm run electron:install
```

## 当前后端边界

仓库默认 `life_coach.main:app` 当前公开健康检查和能力发现，但尚未组合挂载 Sources、Memory、Action 等业务路由。客户端会把“服务在线”和“具体能力可用”分开显示；未挂载功能不会被示例数据或本机状态伪装成服务器能力。
