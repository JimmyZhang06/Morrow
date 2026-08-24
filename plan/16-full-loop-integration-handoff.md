# 单闭环联调收口与遗留问题（2026-08-24）

## 本轮冻结范围

只上线并验证以下闭环：

`记录碎片 → Evidence-backed candidate → 用户确认/纠正/驳回 → 可接受、完成、撤销的小行动`

未扩展回忆录、人生主线、外部 Todo/Calendar。

## 已完成

- 本地 fail-closed 认证、principal、Vault owner membership、ProductionSessionFactory。
- PostgreSQL 17、Alembic 至 `a9c4e7b2d615`、非 owner `life_coach_runtime`。
- 前端配置 Vault ID；请求统一发送 Bearer 与 `X-Vault-ID`；模型 Key 不进入前端。
- Entry 级候选生成 API 在服务端解析当前 SourceFragment，不向客户端暴露内部 fragment ID。
- 唯一 `GovernedModelGateway`；Source/Consent/Knowledge adapter；严格结构化输出与最多一次 repair。
- `DeterministicFakeProvider` 完整闭环与稳定幂等回放。
- StepFun Step Plan Chat Completions adapter：固定批准 origin、后端 SecretStr、JSON mode、超时、失败脱敏。
- Memory confirm/correct/reject 接线；真实 Evidence excerpt 按需读取。
- Action `proposed → accepted → completed/revoked`，持久化幂等、ETag/CAS；完成后仍可撤销。
- `scripts/dev-start.ps1`、`dev-stop.ps1`、`dev-canary.ps1`、`e2e-canary.ps1`。

## 真实验证证据

- 本地 API：`127.0.0.1:18000`；前端：`127.0.0.1:15173`（8000/5173 被未知进程占用，未终止）。
- 基础 canary：合法身份 200；坏 token 401 + Bearer；非 member Vault 404；日志无本地 token/数据库密码。
- E2E canary：候选成功；同幂等键返回同 run/memory；Evidence excerpt 非空；confirm 后 active；Action 实际完成 `proposed → accepted → completed → revoked`。
- 浏览器：实际保存新记录、生成候选、展开逐字原文、确认、创建、接受、完成、完成后撤销。
- PostgreSQL-only 修复：ModelRun UPDATE bind 名与 ORM bulk compiler 冲突；跨用途 consent snapshot 导致 Evidence 被误判 stale。

## 仍未完成（按优先级）

1. **真实 StepFun staging canary**：本机没有 `APP_STEPFUN_API_KEY`，所以没有产生 API 费用。拿到 Key 与一次明确费用授权后，只跑一条合成记录，检查结构化输出、引用、幂等、超时与失败结算。
2. **StepFun 数据处理契约审计**：目前任务按 `provider_managed` retention、`apac` residency、training disabled 配置；上线真实用户数据前，必须用供应商合同/控制台设置确认 retention、训练使用和地域承诺。未确认前只允许合成 canary。
3. **Source 全局 generation 粒度**：当前新增任意 Source 会推进 Vault 级 `source_generation`，使旧 Memory Evidence fail-closed 为 stale。安全正确但产品体验过于保守；后续应设计文档/片段级 invalidation，而不是放宽校验。
4. **真实 provider 故障演练**：单元测试覆盖 adapter 脱敏、runtime timeout/unknown/rollback；真实网络超时与 StepFun 5xx 只能在 Key 授权后做一次受控演练。
5. **对象存储与删除 worker**：不在本轮单闭环阻断范围；真实对象存储 canary、deletion/outbox worker 仍需按此前计划完成。
6. **能力描述**：`model_run_receipts=false` 表示没有公共回执读取 API，不表示内部回执未持久化。是否向客户端暴露需单独产品决策。

## 完整度判断

- 当前单闭环（假模型 + 真实 PostgreSQL + 真实浏览器）：约 **95%**。
- StepFun 生产化：约 **80%**（adapter/治理/Secret 边界完成，真实授权 canary 与供应商数据契约未完成）。
- 更完整产品后端：约 **75%**（删除/outbox、真实对象存储及长期运维能力尚未全部收口）。
