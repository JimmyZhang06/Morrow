# Vistora Desktop v0.5 可分发版本

日期：2026-08-25

## 目标

把桌面端从“开发机上可运行的 Electron 前端”升级为“发给朋友即可使用的 Windows 应用”。接收者不需要安装 Node.js、Python、PostgreSQL，也不需要启动命令行服务。

## 发行架构

- Electron 渲染进程继续负责界面与离线草稿；
- Electron 主进程内置本地数据服务，通过既有隔离 IPC 接口响应请求；
- 数据写入当前 Windows 账户的 Vistora 应用数据目录；
- 默认地址为 `vistora://local`，仅在主进程内部解析，不开放网络端口；
- 设置页仍允许高级用户切换到外部 FastAPI；
- 外部生产后端的 PostgreSQL、认证和模型运行契约不被桌面适配层削弱。

## 内置能力

- 健康检查与能力发现；
- 记录新增与幂等重试；
- 记录列表、详情与重启后恢复；
- 追加修订与版本冲突检测；
- 删除记录；
- 损坏数据文件的恢复副本保留。

## 诚实边界

- 内置服务不声称提供 AI 认识、Memory Verdict 或远程 Action API；
- 认识页示例继续明确标注为界面示例；
- 行动候选仍由桌面本机状态管理；
- 如需真实模型整理，必须连接具有相应能力的外部后端。

## 交付物

- 单文件便携版 `Vistora-0.5.0-x64.exe`；
- 普通用户安装版 `Vistora-Setup-0.5.0-x64.exe`；
- 安装与卸载均不主动删除用户记录。

## 最终验收

- 内置服务契约测试通过：新增、列表、修订、冲突、重启恢复、删除；
- Electron 主进程与内置服务语法检查通过；
- TypeScript 类型检查与 Vite 生产构建通过；
- `app.asar` 已确认包含 `electron/local-backend.cjs`；
- 便携包与安装包通过 7-Zip 完整性测试；
- `win-unpacked/Vistora.exe` 已使用隔离用户目录独立启动；
- 首次启动自动创建 `data/vistora-local-data.json`；
- 便携版与安装版当前均未进行商业代码签名。
