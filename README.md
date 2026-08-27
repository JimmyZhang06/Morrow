# Morrow

> A private, evidence-backed life model that stays under the user's control.

Morrow 是 Vistora 的开源核心：一个帮助用户长期记录生活片段、形成可核对的候选认识，并把认识转化为可撤销小行动的个人生命模型。

它不会把模型生成的内容直接写成“关于你的事实”。系统始终保持四层分离：

```text
Source（用户原始记录）
  → Derived（AI 候选认识 / 人生主线 / 回忆录草稿）
  → Evidence（可追溯的材料引用）
  → Verdict（用户确认、纠正、驳回或撤销）
```

> [!IMPORTANT]
> Morrow 不是医疗、心理诊断或危机干预工具。AI 输出只是基于有限记录的暂定解释，用户始终拥有最终裁定权。

## 当前能力

- 记录创建、读取、修订、删除与版本冲突保护；
- Principal、Vault membership 与请求级数据隔离；
- Memory Inbox：候选认识、支持材料、反例及用户裁定；
- 受治理的 AI 调用：Consent snapshot、权威 Source、ModelRun lineage 与幂等回执；
- StepFun Step Plan provider，以及无外部费用的 deterministic fake provider；
- 从用户认可的认识生成 1–3 条人生主线候选；
- 生成带材料引用和不确定性声明的单章回忆录草稿；
- 创建、确认、撤销日历候选，并在确认后导出 `.ics`；
- 可接受、完成和撤销的小行动闭环；
- Electron + React + TypeScript 的 Vistora Windows 桌面客户端；
- PostgreSQL RLS、非 owner runtime role、Alembic migration rehearsal 和完整测试基线。

## 设计原则

1. **原话优先**：Source 只证明“用户曾这样记录”，不证明外部世界的客观事实。
2. **推断必须可见**：模型输出必须标记为候选，并附上可核对的依据与不确定性。
3. **用户拥有最终解释权**：确认、纠正、驳回和撤销都是一等操作。
4. **AI 不能越权**：业务模块不能直接调用 provider，所有模型请求必须经过 `GovernedModelGateway`。
5. **外部动作必须二次确认**：模型不能自行选择账号、授权范围或写入第三方日历。
6. **隐私是数据结构，不是文案**：Vault scope、consent、lineage、RLS 和删除传播共同构成信任边界。

## 架构概览

```mermaid
flowchart LR
    UI[Vistora Desktop] --> API[FastAPI]
    API --> AUTH[Principal + Vault Membership]
    API --> SRC[Source Service]
    API --> MEM[Knowledge / Memory]
    API --> NAR[Narrative + Action]
    MEM --> GW[Governed Model Gateway]
    NAR --> GW
    GW --> CONSENT[Consent + Policy Snapshot]
    GW --> PROVIDER[StepFun / Deterministic Fake]
    GW --> RECEIPT[ModelRun + Artifact Lineage]
    SRC --> PG[(PostgreSQL + RLS)]
    MEM --> PG
    NAR --> PG
    RECEIPT --> PG
```

主要目录：

```text
apps/desktop/          Electron + React 桌面客户端
src/life_coach/        FastAPI、领域模块、AI 治理与平台代码
alembic/               PostgreSQL schema migrations
tests/                 单元、契约、安全与 PostgreSQL 集成测试
scripts/               本地启动、停止和 E2E canary
plan/                  架构决策、交付计划与已知问题
```

## 技术栈

- Python 3.12+
- FastAPI / Pydantic 2
- SQLAlchemy 2 / Alembic
- PostgreSQL 17 / pgvector / RLS
- React 19 / TypeScript / Vite / Electron
- pytest / Ruff / mypy

## 本地启动（Windows）

### 前置条件

- Python 3.12+
- Node.js 20+
- Docker Desktop
- PowerShell 7（推荐）

### 安装依赖

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"

cd apps\desktop
npm install
cd ..\..
```

### 启动完整本地栈

```powershell
.\scripts\dev-start.ps1 -ApiPort 8000 -FrontendPort 5173
```

打开 <http://127.0.0.1:5173>。脚本会启动 PostgreSQL、执行 Alembic migration、创建本地 principal 与 Vault membership，然后启动 FastAPI 和前端。

停止服务但保留数据库卷：

```powershell
.\scripts\dev-stop.ps1
```

运行本地连通性检查：

```powershell
.\scripts\dev-canary.ps1 -ApiPort 8000
```

本地生成的密码、令牌和数据库数据存放在被 Git 忽略的 `.data/` 中。

## AI Provider

默认使用 `deterministic-fake`，适合本地开发和无费用测试。使用 StepFun 时，只在后端环境或 Secret Manager 中配置：

```dotenv
APP_MODEL_PROVIDER=stepfun-step-plan
APP_STEPFUN_BASE_URL=https://api.stepfun.com/step_plan/v1
APP_STEPFUN_MODEL=step-3.7-flash
APP_STEPFUN_API_KEY=replace-with-backend-secret
APP_MODEL_RUN_HMAC_KEY=replace-with-at-least-32-random-bytes
```

`APP_STEPFUN_API_KEY` 禁止进入前端构建、Git、日志或 ModelRun 回执。可复制 [`.env.example`](./.env.example) 了解其他后端配置项。

## 开发与验证

后端：

```powershell
.venv\Scripts\ruff.exe check src tests alembic scripts
.venv\Scripts\mypy.exe src\life_coach
.venv\Scripts\pytest.exe
```

前端：

```powershell
cd apps\desktop
npm run typecheck
npm run build
```

隔离 PostgreSQL staging 演练会创建并删除一个唯一命名的临时数据库：

```powershell
$env:TEST_POSTGRES_DSN='postgresql+asyncpg://admin:password@127.0.0.1:5432/postgres'
.venv\Scripts\pytest.exe `
  tests/platform/test_alembic_staging_integration.py `
  tests/platform/test_postgres_security_integration.py
```

不要把 `TEST_POSTGRES_DSN` 指向包含真实用户数据的数据库。

## 当前边界

- 人生主线仍是 generation 级候选，逐主题确认、纠正与版本历史尚待实现；
- 回忆录目前按单章生成，不支持整本导出；
- 日历功能目前提供确认后的 `.ics` 导出，尚未实现 Google / Microsoft Calendar OAuth 与远端撤销；
- 内置桌面数据服务与完整 FastAPI 能力仍在进一步收敛；
- 项目处于活跃开发阶段，数据库迁移和公开 API 尚不承诺稳定兼容。

实现进度与已知问题见 [`plan/09-implementation-status-and-known-issues.md`](./plan/09-implementation-status-and-known-issues.md)，叙事功能交付记录见 [`plan/24-narrative-memoir-calendar-delivery.md`](./plan/24-narrative-memoir-calendar-delivery.md)。

## 安全与隐私

- 不要提交 `.env`、`.data/`、数据库导出、用户日记或 API Key；
- 生产 migration identity 与 runtime login 必须分离；
- 生产请求必须使用非 owner、`NOBYPASSRLS` 的 runtime role；
- 发现安全问题时，请不要在公开 Issue 中附上秘密、真实记录或可复现的用户数据。

## 贡献

欢迎提交 Issue 和 Pull Request。提交前请确保后端测试、Ruff、mypy、前端 typecheck 与 build 全部通过，并保持 Source / Derived / Evidence / Verdict 的边界不被绕过。

## License

本项目采用 [Apache License 2.0](./LICENSE)。你可以使用、修改和分发本项目，但必须保留许可证与版权声明。第三方依赖仍适用各自的许可证。
