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

## 2026-08-24 时点遗留项（按优先级）

1. **真实 StepFun staging canary（已于 2026-08-25 完成）**：当时本机没有 `APP_STEPFUN_API_KEY`，所以尚未产生 API 费用；最新状态见下方收口记录。
2. **StepFun 数据处理契约审计**：目前任务按 `provider_managed` retention、`apac` residency、training disabled 配置；上线真实用户数据前，必须用供应商合同/控制台设置确认 retention、训练使用和地域承诺。未确认前只允许合成 canary。
3. **Source 全局 generation 粒度**：当前新增任意 Source 会推进 Vault 级 `source_generation`，使旧 Memory Evidence fail-closed 为 stale。安全正确但产品体验过于保守；后续应设计文档/片段级 invalidation，而不是放宽校验。
4. **真实 provider 故障演练**：单元测试覆盖 adapter 脱敏、runtime timeout/unknown/rollback；真实网络超时与 StepFun 5xx 只能在 Key 授权后做一次受控演练。
5. **对象存储与删除 worker**：不在本轮单闭环阻断范围；真实对象存储 canary、deletion/outbox worker 仍需按此前计划完成。
6. **能力描述**：`model_run_receipts=false` 表示没有公共回执读取 API，不表示内部回执未持久化。是否向客户端暴露需单独产品决策。

## 完整度判断

- 当前单闭环（假模型 + 真实 PostgreSQL + 真实浏览器）：约 **95%**。
- StepFun 生产化：约 **80%**（adapter/治理/Secret 边界完成，真实授权 canary 与供应商数据契约未完成）。
- 更完整产品后端：约 **75%**（删除/outbox、真实对象存储及长期运维能力尚未全部收口）。

---

# StepFun 长期合成笔记 canary 收口（2026-08-25）

## 本轮结果

- 已在后端私密环境接入 StepFun Step Plan，模型为 `step-3.7-flash`；Key 未进入前端、Git、模型回执或应用日志。
- 新增 `scripts/longitudinal-ai-canary.ps1`，可创建或复用 2025 年 1 月至 8 月的 6 条虚拟长期笔记，并逐条执行真实候选生成、幂等回放和 Evidence 原文校验。
- 真实 canary 最终结果：**6 条历史笔记 → 6 个候选认识 → 6 个唯一 Memory**；每条幂等回放均返回原结果，每段 Evidence excerpt 均可在对应 Source 中逐字定位。
- 桌面端另用第 7 条中文笔记执行真实模型测试：约 18 秒完成，候选保持中文；界面可展开 1 条授权 Source 的逐字原文。由此确认候选请求的 60 秒前端等待窗口有效。

## 本轮暴露并修复的问题

- StepFun 偶发返回空 `content`、推理内容被长度截断、Markdown JSON fence 或网络超时。adapter 现在使用脱敏且有界的 failure code，失败继续 fail-closed，不记录响应正文或异常文本。
- 输出上限由 1200 调整为 4096 tokens；仅接受纯 JSON 或一个完整、无额外文本的 JSON fence，之后仍执行严格 Pydantic schema 校验。
- 后端真实 provider 超时调整为 45 秒；仅候选生成请求的前端/Electron 等待窗口调整为 60 秒，普通请求仍保持 8 秒。
- Prompt 升级为 `candidate-insight-v2`，要求所有自然语言字段与 Source 同语种；中文 Source 生成简洁中文结果。旧的 v1 英文候选不做静默重写。
- 真实 PostgreSQL staging 演练暴露了测试写死旧 migration revision 的问题；断言已改为从 Alembic 配置读取当前唯一 head。`upgrade → downgrade → upgrade` 与非 owner trust-boundary 两个集成测试均通过。

## 仍需明确的产品能力缺口

当前 `candidate-insight` 任务仍以**单条 Entry** 为输入。上述 6 条跨期笔记验证了长期数据的存储、逐条推理、证据追溯和幂等性，但尚未实现真正的跨 Entry 聚合、相似候选合并、矛盾检测或随时间更新的“人生模式”。因此不能把“6 个唯一 Memory”描述为已完成长期人生主线识别。

下一阶段如继续收敛长期认知，优先实现一个受治理的 `longitudinal-synthesis` 任务：仅检索用户授权的多条 Source，输出带多 Source evidence 的候选模式；保持用户裁定、版本化和可撤销，不直接写成既定人格结论。

## 更新后的完整度判断

- 单闭环（真实 PostgreSQL + 真实 StepFun + 真实桌面端）：约 **97%**。
- StepFun 工程接入：约 **90%**；剩余主要是供应商数据处理契约审计与更系统的真实故障演练。
- 长期自我认知能力：仍处于**数据基础与单条候选阶段**；跨记录综合尚未实现。

## 非阻断工程债

- 全量 pytest 仍报告 FastAPI/Starlette `TestClient` 的 `httpx` 迁移提示；当前不影响行为，但依赖升级前应迁移到新版测试客户端接口。
- Alembic 配置尚未显式设置 `path_separator=os`，staging 演练会产生兼容性 deprecation warning；后续配置维护时一并处理。
