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

