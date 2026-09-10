# Morrow Desktop

Windows x64 桌面客户端，当前版本 **0.6.0-beta.5**。Electron + React + TypeScript，内置 Python/FastAPI 和 PostgreSQL。本地保存记录，在线 AI 使用用户自己的模型 API。

## 使用与构建

完整依赖安装、开发和构建步骤见 [根目录 README](../../README.md)，使用说明见 [当前版本说明](../../release-notes/0.6.0-beta.5.md)。

在完成根目录 Python 环境配置后：

```powershell
npm ci
npm run icons
npm run build
npm run dist:share
```

输出 `release/Morrow-<version>-x64.exe` 和 `release/Morrow-Setup-<version>-x64.exe`。安装包不进入 Git。生成图标必须先于前端构建：前端、窗口与 Windows 图标共同使用 `resources/morrow-icon-source.png` 的生成结果。

## 常用脚本

| 命令 | 用途 |
| --- | --- |
| `npm run dev` | 启动 Vite 与 Electron 开发窗口 |
| `npm run icons` | 由生图原稿生成 PNG、ICO 和 favicon |
| `npm run build` | TypeScript 检查与前端构建 |
| `npm run build:runtime` | 校验 PostgreSQL 下载，打包 Python 后端 |
| `npm run dist:share` | 完整验证及安装版、便携版构建 |
| `npm run test:local-backend` | 旧版 JSON 契约、写入失败与分页保护 |
| `npm run test:managed` | 备份加密、模型配置及密钥隔离 |
| `npm run test:managed:integration` | 已打包后端与真实 PostgreSQL/RLS 验证 |
| `npm run test:electron:release` | 实际打包桌面端的记录、多模型、恢复、重启验证 |
| `node scripts/test-electron-release.mjs --clean-only` | 验证品牌、旧目录隔离和空白首次启动 |
| `node scripts/test-managed-runtime.mjs --integration --mock-ai` | 模拟 HTTP 的候选→证据→确认→行动完整链路 |
| `node scripts/release-report.mjs` | 两个 EXE 的校验值、已知凭据及用户数据文件扫描 |

## 数据行为

- 发行版同时将数据库配置与 Chromium sessionData 隔离到版本目录。新版默认空白；同一版本重启保留之后写入的记录。重新安装同一版本不会清空该版本的数据。
- 旧数据保留但不自动导入；迁移需要用户从旧版导出加密备份并主动恢复。
- 模型配置以卡片展示，可新增、选择编辑、保存切换和删除。删除当前配置会关闭 AI，删除其他配置不会重启当前服务。
- API Key 由 Windows 当前账户加密保护，不返回给渲染进程，也不进入备份。地址或协议变化必须重新输入密钥。
- 备份保留 `.vistora` 扩展名以兼容已有格式。内部接口标识和数据库名称不等同于产品显示名称。
- 新版本无默认模型配置或测试记录。开发及验收使用隔离目录中的模拟记录，不打包进发行文件。

详见 [多模型与版本隔离契约](../../plan/27-desktop-model-profiles.md)。
