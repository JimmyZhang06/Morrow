# Life Coach Backend

这是一个“由用户校订、可追溯、可撤回”的个人生命模型后端。当前处于 MVP 基础实现阶段。

架构与实施约束以 [`plan/README.md`](./plan/README.md) 为准。尤其需要保持四层分离：

```text
Source（用户记录）
→ Derived（AI 候选）
→ Evidence（精确出处）
→ Verdict（用户裁定）
```

## 技术基线

- Python 3.12+
- FastAPI / Pydantic
- SQLAlchemy 2 / Alembic
- PostgreSQL + pgvector
- PostgreSQL jobs/outbox
- pytest / Ruff / mypy

本仓库中的 Source 只证明“用户曾这样记录”，不被当作对客观历史的证明。任何 AI 推断都不能直接写成用户身份事实。

## PostgreSQL staging 演练

`TEST_POSTGRES_DSN` 必须指向允许创建临时数据库和角色的隔离 PostgreSQL 实例。测试会创建并删除唯一命名的 sibling database，执行 `upgrade → downgrade → upgrade`，再切换到非 owner 的 `life_coach_app` 角色验证 RLS 与 membership 只读边界：

```powershell
$env:TEST_POSTGRES_DSN='postgresql+asyncpg://admin:password@127.0.0.1:5432/postgres'
.venv\Scripts\python.exe -m pytest `
  tests/platform/test_alembic_staging_integration.py `
  tests/platform/test_postgres_security_integration.py -q
```

禁止把该变量指向包含用户数据的数据库。生产 migration identity 与 runtime login 必须分离；运行请求只允许切换到 `NOLOGIN NOBYPASSRLS` 的业务角色。
