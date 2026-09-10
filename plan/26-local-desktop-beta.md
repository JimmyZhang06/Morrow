# Vistora 本地 Windows 试用版

日期：2026-09-10。目标版本：0.6.0-beta.1。

## 本轮发行契约

- 供少量受邀用户试用；不部署服务器、不开放公共注册。
- 随包携带 Python 后端与 PostgreSQL 17，用户无需 Node、Python、Docker 或数据库安装。
- 数据服务只监听 127.0.0.1；每个配置目录生成独立身份、数据库密码和 API 令牌。渲染进程不接收这些凭证。
- 新增 desktop 环境，保留完整 Source、Evidence、Verdict、ModelRun、RLS 边界；生产服务原有安全配置不放宽。
- API Key 默认未配置，在线 AI 默认关闭。首次明确同意后方可启用 StepFun Step Plan；调用可能产生用户账户费用。
- Windows safeStorage 保护本地密钥材料；这不是整个数据库或浏览器草稿的全盘加密，也不是端到端加密。
- 应用不包含共享供应商密钥。当前仅支持已有的 Step Plan adapter，不宣称支持所有 OpenAI-compatible 服务。

## 用户路径

1. 安装或启动 portable，等待首次私人空间初始化。
2. 直接记录；在“设置与隐私”中阅读在线 AI 说明并填写自己的 Step Plan API Key。
3. 从记录请求候选，核对依据并确认或修正，再选择小行动。
4. 从设置导出密码加密备份，密码至少 12 字符；保管到另一个位置。
5. 需要恢复时选择备份并再次确认。先还原到新数据库，通过启动检查后切换；失败返回旧库。

## 升级与恢复边界

- 旧版 JSON 记录及其修订通过真实 API 幂等迁移，逐条核对后记迁移标记。原始文件保留。
- 备份包含数据库、解密所需身份材料和本机草稿，不含在线 AI 密钥；AES-256-GCM 校验完整性。
- 恢复不会覆盖原数据库；在线 AI 关闭并清除供应商密钥，需重新启用。
- 备份、旧版原始文件、恢复前数据库不会随单条记录删除而清除。试用者需知悉这些保留副本。
- 暂无自动更新、自动备份或旧副本清理界面。升级前须手工导出有效备份。
- 本轮不宣称用户已完成复盘；行动“完成”仅表示用户标记完成。

## 可重复验证

```powershell
.venv/Scripts/python.exe -m pip install -r requirements-desktop-build.txt
.venv/Scripts/python.exe -m pip install --no-deps -e .
cd apps/desktop
npm ci
npm run dist:share
node scripts/test-electron-release.mjs
```

PostgreSQL archive 来源为 PostgreSQL 官方 Windows 下载页链接的 EDB：
https://www.enterprisedb.com/download-postgresql-binaries

固定文件：postgresql-17.11-3-windows-x64-binaries.zip。SHA-256 为本次通过官方 HTTPS 下载后记录的构建校验值，不是独立供应商签名。

`4b8db0930c38f6ef845db919551dedda3b6b845aeb0927b3d79a6e8e9e4537cf`

CI 只构建和保留产物，不自动公开发布。商用代码签名、另一台干净 Windows 机器、真实用户反馈仍需发布负责人核验。

## 本次验证记录

- 后端：1064 passed，默认运行跳过的 2 项真实 PostgreSQL 测试已在独立内置数据库中另外通过。
- Ruff、strict mypy、桌面 TypeScript 和 Vite 生产构建通过；npm audit 无已知漏洞。
- 独立可执行后端 + PostgreSQL：初始化、认证拒绝、记录读写、旧 JSON 修订迁移、加密备份、错误密码拒绝、恢复、重启通过。
- 实际打包 Electron：首次启动、Windows safeStorage 密文文件、存储配额失败保留输入、记录保存、设置、备份/恢复按钮、恢复后刷新、重启通过。
- 真实在线 AI 未通过：现有测试密钥被供应商 HTTP 400 拒绝，原因为没有有效 Step Plan 订阅。没有把拒绝状态视为成功；需要有效订阅后重新运行 `--ai` 验证候选、证据、确认和行动。
- 验证均使用隔离的合成数据目录，没有迁移或覆盖用户现有数据。
- CI 工作流已写入仓库，尚未在远端执行。安装包尚未在第二台干净 Windows 设备验证，也未进行商业代码签名。
- 最终便携版与 NSIS 安装版均通过 7-Zip 完整性检查；Windows Defender 自定义扫描完成，没有匹配这两份产物的威胁检测。已配置凭证的明文扫描通过，SHA-256 清单保存在 `apps/desktop/release/SHA256SUMS.txt`。
- 便携版 SHA-256：`27550c26edf741b00645bcfebeb9c88659d768d6772d52a2d6192adeb4e45d2e`。
- 安装版 SHA-256：`1d29a4f97c0551cdd47dd7a175f5627a1650ff72f63091b0b8a58a15ab0d58e6`。
