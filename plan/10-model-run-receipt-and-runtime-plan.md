# 下一阶段计划：ModelRun Receipt 与受治理运行时

更新日期：2026-08-24
状态：批次 3.1 已实现；批次 3.2 待执行
对应总路线：`09` 文档中的第 3 项后半段

## 1. 本阶段只解决什么

把当前“调用前已校验”的 `GovernedModelGateway`，收敛为一条**可追溯、短事务、默认拒绝、不会持久化用户正文**的模型调用路径。

本阶段的完成结果是：每次模型调用都存在一张权威的 `ModelRun` 技术收据，能回答：

- 哪个 Vault、哪个已注册任务发起了调用；
- 使用了哪个 provider/model/region 与哪个版本化 schema；
- 调用依据的是哪一版 consent、`policy_epoch` 与 `source_generation`；
- 哪些 Source fragment 被授权使用；
- 是否真的开始了外部 I/O，最终成功、失败，还是结果未知；
- 生成的候选认识是否确实来自该次运行。

收据不能回答、也不得保存：用户原文、拼接后的 prompt、原始模型输出、provider 错误正文、密钥、access token 或可逆内容副本。

## 2. 为什么它是现在的下一步

当前已经具备：

- 唯一 provider kernel；
- 服务器注册的模型任务；
- Source/Consent 权威读取与 I/O 前 fence 复验；
- 严格结构化输出和敏感错误脱敏；
- 非 owner PostgreSQL/RLS 测试基线。

但还缺两块生产必需能力：

1. `model_run_id` 目前没有对应的权威表，无法证明一次派生结果经过了哪次受治理调用。
2. provider I/O 发生在调用方数据库事务内，真实网络延迟会制造长事务、连接占用和撤销竞态。

因此，先接一家真实 provider 会得到“能够调用、但难以审计和恢复”的路径；先接异步 worker 又会迫使系统提前持久化可重放的明文输入。首个批次应先解决收据和短事务边界。

## 3. 目标运行协议

```text
业务命令
   |
   v
[A. prepare 短事务]
读取 live Source + consent + provider policy
创建 authorized ModelRun + ModelRunInput 技术引用
提交
   |
   v
[B. dispatch 短事务]
重新读取 consent / policy_epoch / source_generation / tombstone
CAS: authorized -> dispatching
提交
   |
   v
[C. 无数据库事务的 provider I/O]
仅在进程内持有本次明文；超时和取消默认失败
   |
   v
[D. finalize 短事务]
CAS: dispatching -> succeeded | failed | unknown
成功时与候选认识及 evidence/model_run_id 原子落库
```

第一版保持**进程内同步执行**。它已经能移除长数据库事务，又不需要把 prompt 或明文输入放进 Job/outbox。只有当闭环吞吐量证明需要异步化时，才设计短期加密 input artifact 与 worker 恢复协议。

`B` 到 `C` 之间仍存在无法完全消除的极小撤销竞态。实现必须记录复验时间并把窗口缩到一次本地提交之后立即 I/O；不能宣称强一致撤销已经覆盖已经发出的网络请求。

## 4. 数据模型草案

### 4.1 `model_run`

| 字段组 | 建议字段 | 约束 |
|---|---|---|
| 身份 | `id`, `vault_id`, `task_type`, `idempotency_key` | UUID；Vault 复合外键与 FORCE RLS；同一 Vault/任务内幂等键唯一 |
| 模型路由 | `provider`, `model`, `model_revision`, `data_residency` | 全部是受限技术标识；组合必须命中 capability registry |
| 版本 | `prompt_template_version`, `schema_version`, `pipeline_version` | 不保存模板正文和 schema body |
| 授权 | `consent_snapshot_id`, `policy_epoch`, `source_generation`, `actual_sensitivity`, `retention_policy` | 由权威 adapter 铸造；非负 fence |
| 指纹 | `request_hash`, `task_definition_hash` | 使用现有 Vault HMAC 类型；禁止普通 SHA-256 对低熵内容充当隐私保护 |
| 状态 | `state`, `attempt`, `dispatch_generation` | CAS 更新；计数非负 |
| provider receipt | `provider_request_id`, `safe_error_code` | 可空、长度有界、只接受技术字符；不保存错误消息正文 |
| 时间 | `authorized_at`, `dispatch_started_at`, `dispatch_expires_at`, `io_finished_at`, `completed_at` | 状态对应时间由 DB CHECK 约束；过期 dispatch 可收敛为 `unknown` |

建议状态：

```text
authorized -> dispatching -> succeeded
                         -> failed
                         -> unknown
authorized -> denied | canceled
```

- `denied` 只表示 prepare 后、真正 I/O 前的权威复验失败。
- `failed` 表示已知没有得到可接受结果，例如明确的 provider 失败或 schema 验证失败。
- `unknown` 表示网络结果无法判定；不能自动伪装成成功，也不能无界重试。
- 第一版不实现 `unknown` 的 provider 对账；仅保留人工/运营处置入口和指标。只有 provider 提供可靠 request lookup 后才增加 reconciliation。

### 4.2 `model_run_input`

每个输入只保存：`vault_id`, `model_run_id`, `kind`, `object_id`, `content_fingerprint`, `ordinal`。

- `object_id` 是 Source fragment 等技术 UUID，不复制正文。
- `content_fingerprint` 绑定 prepare 时验证过的内容版本，但持久化值必须是 Vault-scoped HMAC；不得复制可跨 Vault 关联的 raw SHA-256。
- `(vault_id, model_run_id, kind, object_id)` 唯一。
- 该表和 `model_run` 一起受 Vault RLS 与删除策略控制。

### 4.3 派生对象引用

把 Knowledge 中已有的 nullable `model_run_id` 收紧为 Vault 复合外键时，要分两步迁移：

1. 新运行先写权威 `ModelRun`，新派生对象必须引用它。
2. 盘点历史 nullable/孤立值；在没有真实历史收据时不得伪造补录。完成数据清理后再添加外键。

首个合并批次只建立新表和 repository，不立即对所有既有列加硬外键，避免一次迁移扩大爆炸半径。

## 5. 分批实施

### 批次 3.1：持久化收据与状态机

范围：

- 新增 `ModelRunState`、`ModelRun`、`ModelRunInput`；
- 新增 vault-scoped repository：prepare、claim dispatch、mark succeeded/failed/unknown；
- 所有状态变化使用 `vault_id + id + expected_state + dispatch_generation` CAS；
- 新增 Alembic migration、RLS policy、runtime role 权限和 metadata 注册；
- 为 request/task fingerprint 复用现有 Vault HMAC 能力；
- 增加 SQLite 领域测试与真实 PostgreSQL 非 owner 集成测试。

完成门槛：

- 跨 Vault 读取、写入和 CAS 全部失败；
- 重复 prepare 幂等，重复 finalize 不会覆盖首个终态；
- receipt/outbox/log/异常中扫描不到测试正文；
- migration `upgrade -> downgrade -> upgrade` 通过；
- 不引入真实 provider，也不改变现有闭环行为。

该批次已于 2026-08-24 完成实现和本地审计。

实际结果：

- 新增 `ModelRun`、append-only `ModelRunInput` 和新 Alembic head `d4c8a1e7f302`；
- receipt 可由业务角色 `SELECT/INSERT/UPDATE`，但没有 `DELETE`；input 只有 `SELECT/INSERT`；
- repository 已覆盖 prepare 幂等、dispatch CAS、generation-exact finalize 与过期 dispatch → `unknown`；
- 输入、请求与任务指纹均使用 Vault HMAC，数据库中没有 prompt、正文、原始输出或 provider error body 字段；
- 一次性 PostgreSQL 16 中的迁移往返和非 owner RLS/CAS 集成测试通过。

### 批次 3.2：Gateway 短事务编排

范围：

- 将当前 `run(session=...)` 拆为 prepare、dispatch revalidation、provider I/O、finalize 四段；
- 增加生产 session factory 驱动的 application orchestrator，业务层不再把一个 Session 横跨 provider I/O；
- 将 `ModelRunSpec.run_id` 固定为已持久化 receipt ID；
- 用 fake provider 验证 timeout、取消、schema failure、repair attempt 与 `unknown`；
- 成功结果与候选认识/evidence 在 finalize 事务内绑定 `model_run_id`。

完成门槛：

- provider 阻塞时数据库中不存在仍打开的业务事务；
- consent 撤销、Source tombstone 或 fence 变化发生在 dispatch 前时，provider 调用次数保持为 0；
- 终态和候选对象不存在“一个成功、另一个缺失”的半写状态；
- 仍只有 `ai/provider.py` 能调用 `provider.complete()`。

### 批次 3.3：单一真实 provider + 真实明文读取 canary

只有 3.1、3.2 稳定后再开始：

- 只选择一家 provider、一个模型、一个 region、一个结构化输出任务；
- provider profile 明确 retention、training use、region、timeout 与支持的幂等/查询能力；
- 配置默认 `disabled`，用显式 feature flag 和合成数据 canary 开启；
- 实现 KMS envelope decryption + object version/AAD 完整性校验的 `SourceFragmentPlaintextReader`；
- canary 只使用合成碎片，验证对象上传、加密、读取、模型调用和删除，不使用真实用户内容；
- provider 响应先严格解析，原始响应不落库，适配器错误只映射为固定错误码。

provider、KMS 和对象存储厂商尚未确定；厂商选择与凭据注入不在 3.1 中假定。

## 6. 测试矩阵

| 类别 | 必测场景 |
|---|---|
| 状态机 | 合法转换、非法跳转、并发 claim、过期 generation、终态不可覆盖 |
| 授权 | consent 缺失/过期/撤销、provider-region 不匹配、retention 超标、training use 不允许 |
| Source | 非 current revision、tombstone、content fingerprint 不符、跨 Vault fragment |
| 故障 | provider exception、timeout、取消、非法 JSON、schema repair 耗尽、完成事务失败 |
| 隐私 | DB、Job/outbox payload、日志、trace、异常不含正文/prompt/output/provider error body |
| PostgreSQL | 非 owner FORCE RLS、连接复用、复合外键、CAS 竞争、迁移往返 |
| 架构 | provider 调用唯一出口；业务层不能自铸 provider/consent/fence/run receipt |

## 7. 本阶段明确不做

- 多 provider 路由、自动 fallback、流式输出、tool calling；
- 任意自由 prompt 或用户选择模型；
- 为模型输入建立可长期重放的明文队列；
- 回忆录章节、人生主线、外部 Todo/Calendar；
- deletion/outbox worker 的完整实现；
- 把模型调用误当作需要用户单次授权的外部 Todo 副作用。

删除 worker 与真实对象存储 canary 仍是总路线第 4 项。3.3 只完成最小真实存储/解密 canary，以证明 Model Gateway 的输入边界可用，不顺带扩成完整删除系统。

## 8. 开始实施前需要冻结的决策

3.1 可以直接开始，不依赖厂商选择。开始 3.3 前必须明确：

1. provider/model/region 及其真实数据保留、训练用途与 request lookup 能力；
2. 对象存储和 KMS，以及对象版本、AAD 与密钥轮换约定；
3. `ModelRun` 技术收据的保留期和用户删除时的处理方式；
4. timeout 后是否能依赖 provider request ID 对账；不能则保持 `unknown` 并禁止自动重试。

## 9. 建议执行顺序

```text
现在：3.1 schema + repository + migration + 非 owner 测试
  -> 审计并合并
  -> 3.2 短事务编排 + fake provider 故障测试
  -> 审计并合并
  -> 冻结厂商数据处理配置
  -> 3.3 单 provider + KMS/object-store 合成 canary
  -> 回到总路线第 4 项 deletion/outbox worker
  -> 再做第 5 项唯一用户闭环
```

这样每一批都能独立回滚、独立审计，也不会为了“先看到 AI 输出”而跳过最重要的授权与可追溯基础。
