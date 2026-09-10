<p align="center">
  <img src="apps/desktop/resources/morrow-icon-source.png" width="112" alt="Morrow 应用图标" />
</p>

# Morrow

**把生活记录下来，把关于自己的理解留给自己确认。**

Morrow 是一个在 Windows 本机运行的个人记录与自我理解应用。你可以写下生活片段，让 AI 提出带原话依据的候选认识，再亲自确认、修正或拒绝，并尝试一个可撤销的小行动。

当前版本：**0.6.0-beta.5**，面向少量受邀用户的 Windows x64 试用版。应用内置 FastAPI 和 PostgreSQL，使用者无需安装 Python、Docker 或数据库，也不需要部署服务器。在线 AI 默认关闭，使用用户自行配置的模型 API。

## 已实现

| 功能 | 当前行为 |
| --- | --- |
| 生活记录 | 新增、编辑、删除、草稿、分页读取、版本冲突保护 |
| 候选认识 | AI 输出附带原话证据，由用户确认、纠正、驳回或暂缓 |
| 小行动 | 基于认可的认识生成行动，支持接受、标记完成和撤销 |
| 人生主线与回忆录 | 生成主线候选和带引用、不确定性说明的单章草稿 |
| 多模型配置 | 保存多个服务地址、模型名称及独立密钥，切换或删除配置 |
| 本地运行 | Electron 管理内置 Python 后端和 PostgreSQL，仅监听本机 |
| 备份恢复 | 密码加密的 `.vistora` 备份，包含记录及相关数据，不包含模型 API Key |
| 新版本空白启动 | 新版本不自动读取旧版历史、草稿或模型设置；同版本重启保留新记录 |

Morrow 不把模型生成的内容直接当作“关于你的事实”：

```text
Source（用户原话） → Derived（AI 候选） → Evidence（可核对依据） → Verdict（用户裁定）
```

Morrow 不是医疗或心理诊断工具。模型输出只是对有限材料的暂定解释。

## 使用桌面版

安装包文件名为 `Morrow-Setup-0.6.0-beta.5-x64.exe`，便携版为 `Morrow-0.6.0-beta.5-x64.exe`。本仓库提交源代码和构建脚本，EXE 不放入 Git；可按下方步骤构建，也可运行仓库的 [Windows desktop beta 工作流](https://github.com/JimmyZhang06/Morrow/actions/workflows/desktop-beta.yml) 获取构建产物。工作流成功完成后才会提供下载产物。

1. 安装或打开应用，等待首次创建本地数据空间。
2. 直接记录；未配置 AI 时也能保存。
3. 在“设置与隐私 → 我的模型”新增配置，填写 HTTPS API 根地址、API Key 和模型名称。
4. 阅读数据处理说明，点击“保存并切换到此模型”。设置保存成功不代表供应商账户一定有可用额度。
5. 从已保存记录发起整理，核对证据，再决定是否接受候选认识。

支持 **OpenAI 兼容 Chat Completions API** 和 **Step Plan 专用协议**。应用会在根地址后追加 `/chat/completions`，不要重复填写该路径。仅在供应商支持时启用 JSON 模式。原生 Anthropic、Gemini 等不同协议需要对应的兼容接口，当前不承诺所有 API 都可直接使用。

完整操作说明见 [当前试用说明](release-notes/0.6.0-beta.5.md)。

## 数据、升级与隐私

- 应用和数据库在本机；主动请求在线 AI 时，所选记录及必要材料会发往你配置的供应商，费用由你的模型账户承担。
- 供应商的数据处理地区、保留期限和训练使用规则适用；通用 API 不代表零保留或完全离线。
- 模型密钥和应用数据密钥使用 Windows 当前账户的 `safeStorage` 加密保护，不通过界面状态返回。部分草稿和派生数据并非全盘加密。
- **每个新版本首次打开为空白。** 数据和浏览器缓存按版本隔离，不自动导入旧记录。旧目录保留作回退；要迁移，请在旧版导出加密备份，再在新版主动恢复。
- 同一版本重新打开、重新下载或重新安装，会保留该版本已经保存的新记录。
- 备份密码至少 12 个字符，无法找回。恢复后 AI 关闭，模型密钥需要重新配置。
- 删除记录不会清除旧目录、回退数据库和你导出的备份；这些副本需要自行管理。

## 从源码构建 Windows 版本

建议与 [CI 配置](.github/workflows/desktop-beta.yml) 一致：**Windows x64、Python 3.12.14、Node.js 24、PowerShell**。构建需要网络下载 Electron、Python/npm 依赖和固定版本的 PostgreSQL 运行时。构建安装包不需要 Docker。

在仓库根目录执行：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-desktop-build.txt
.venv/Scripts/python.exe -m pip install --no-deps -e .

cd apps/desktop
npm ci
npm run dist:share
```

`dist:share` 会验证本地数据处理、打包 Python 后端、下载并校验 PostgreSQL、进行数据库集成验证、从生图原稿生成图标，然后构建便携版与安装版。

产物位于 `apps/desktop/release/`。构建后继续验证实际桌面程序并生成校验清单：

```powershell
npm run test:electron:release
node scripts/test-managed-runtime.mjs --integration --mock-ai
node scripts/release-report.mjs
```

校验清单包含 SHA-256、已知本地凭据扫描和用户数据文件扫描结果。运行时缓存位于 `.data/`，不提交 Git。

## 本地开发

先完成上述依赖安装，再生成前端引用的图标：

```powershell
cd apps/desktop
npm run icons
cd ../..
```

开发栈使用 Docker Desktop 启动 PostgreSQL。仓库根目录执行：

```powershell
./scripts/dev-start.ps1 -ApiPort 8000 -FrontendPort 5173
```

打开 `http://127.0.0.1:5173`。脚本执行迁移、创建本地身份与 Vault，并启动 FastAPI 和前端。若要调试 Electron，启动脚本时加 `-SkipFrontend`，再另开终端在 `apps/desktop` 运行 `npm run dev`。

```powershell
./scripts/dev-canary.ps1 -ApiPort 8000
./scripts/dev-stop.ps1
```

开发脚本默认使用无外部费用的 `deterministic-fake`；发行版默认关闭 AI，禁止启用这个模拟供应商。开发配置保存在忽略提交的 `.data/dev/local.env`。后端环境参数见 [`.env.example`](.env.example)。

## 验证命令

仓库根目录：

```powershell
.venv/Scripts/python.exe -m ruff check src tests alembic scripts
.venv/Scripts/python.exe -m mypy src/life_coach
.venv/Scripts/python.exe -m pytest
```

桌面目录：

```powershell
npm run test:local-backend
npm run test:managed
npm run icons
npm run build
npm run test:managed:integration
```

`test:managed:integration` 需要先完成 `npm run build:runtime`，会使用隔离的本地 PostgreSQL 验证迁移和 RLS。`test:electron:release` 需要先生成 `release/win-unpacked/Morrow.exe`，覆盖空白启动、旧目录隔离、记录保存、多模型切换、删除、备份恢复和重启。模拟 HTTP 流程验证不会产生真实模型费用，也不等同于真实供应商质量验收。

## 代码结构

```text
apps/desktop/          Electron、React、桌面运行时与发行脚本
src/life_coach/        FastAPI、领域模块、模型网关、证据与权限检查
alembic/               PostgreSQL 数据库迁移
scripts/               开发启动、运行时打包与集成测试辅助
tests/                后端单元、契约、安全及数据库测试
release-notes/         逐版本使用说明
plan/                  架构决策、交付计划与边界说明
```

所有模型请求经过 `GovernedModelGateway`，结合用户同意、Source 权限、Vault 隔离、模型运行回执与证据验证。PostgreSQL runtime role 为非 owner、`NOBYPASSRLS`；模型不得绕过用户裁定直接确立事实或执行外部动作。

图标由 imagegen 内置生图工具生成：[原稿](apps/desktop/resources/morrow-icon-source.png) · [完整提示词](apps/desktop/resources/morrow-icon-prompt.md)。构建会从原稿生成 PNG、ICO 和 favicon。

## 当前边界

- 当前为未商业签名的受邀试用版，独立干净 Windows 机器的安装升级验收仍待完成。
- 真实供应商调用需要用户提供有效 API 配置和额度；已完成的模拟测试不代表所有模型都兼容。
- 无自动更新、多设备同步、完整复盘编辑器或整本回忆录导出。
- 人生主线仍是候选；日历支持确认后的 ICS 导出，未接入第三方日历 OAuth 写入。
- 不承诺数据库迁移和公开 API 的长期稳定兼容。

更多说明见 [桌面文档](apps/desktop/README.md)、[本地发行契约](plan/26-local-desktop-beta.md) 和 [多模型及版本数据隔离](plan/27-desktop-model-profiles.md)。

## 贡献与许可

提交前运行相关验证，保持 Source / Derived / Evidence / Verdict 的边界。不要提交 API Key、`.env`、`.data/`、数据库导出、备份或真实个人记录。安全问题报告也不要包含这些内容。

代码采用 [Apache License 2.0](LICENSE)，第三方依赖遵循各自许可。
