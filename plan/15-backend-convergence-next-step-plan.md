# 后端下一阶段收敛计划：生产运行时、删除闭环与可撤销微实验

更新日期：2026-08-24  
状态：已执行；联调结论与遗留见 `16-full-loop-integration-handoff.md`  
依据：`09-implementation-status-and-known-issues.md` 当前基线

## 1. 这轮的唯一目标

把现有后端从“领域与测试基线基本完整”推进到一个可上线验证的单用户闭环：

```text
记录一条碎片
  → 生成一个有证据的候选认识
  → 用户确认或纠正
  → 系统提出恰好一个可撤销的微实验
  → 用户接受、跳过或撤销
```

这轮不追求产品功能面完整。回忆录章节、人生主线、主动推送、外部 Todo、Calendar、消息发送和多 provider 路由全部继续延后。

## 2. 当前基线与尚未闭合的地方

已经具备：

- Bearer 认证、`principal-vault membership` 和请求期 `ProductionSessionFactory`；
- Source 创建、修订、读取、删除，以及 Memory inbox、详情、证据摘录、确认和纠正 API；
- 唯一 `ModelGateway`、Source/Consent 权威 adapter、短事务 `GovernedModelRuntime`；
- `candidate_insight` 严格输出协议、确定性 Safety classifier、Knowledge 原子持久化与成功 replay；
- ModelRun receipt/artifact、FORCE RLS、disposable PostgreSQL 非 owner 测试与 Alembic 往返演练；
- Job/outbox/lease/UNKNOWN 的通用基础设施与 Action 的纯领域规则。

尚未闭合：

1. `APP_MODEL_PROVIDER != disabled` 时，应用不会自动构造 Candidate runtime，也不会拒绝启动；配置可能声称启用模型，但公开能力静默缺失。
2. 没有真实 provider、托管 KMS/object-store protector、密钥轮换协议和合成数据 canary。
3. Source 删除只完成数据库 tombstone，没有原子创建删除 outbox，也没有 maintenance worker 擦除对象存储等 sink。
4. Memory verdict 没有幂等键，响应丢失后的安全 replay 不成立。
5. Action 只有纯领域对象，没有 ORM、repository、应用服务、API、RLS、生产 authority 或撤销状态。

## 3. 冻结的产品和架构决策

### 3.1 首版小行动采用“可撤销微实验”

复用 `ActionCandidate.experiment_candidate` 的语义，不使用 `Task` 或 `ExternalAction`：

- `Task` 当前只有 `OPEN`，没有撤销语义；
- `ExternalAction` 会过早引入 connector、外部副作用和对账；
- `ExperimentProposal` 已强制提供 rationale、cost、exit plan，并要求 `is_reversible=True`。

接受微实验只表示“用户愿意尝试”，不会创建 Todo、日历事件、消息、支付或任何外部 I/O。撤销只是内部状态变化，始终可用。

### 3.2 首版微实验不调用第二次模型

候选认识仍由唯一 Model Gateway 生成；微实验由服务器版本化模板根据已确认 Memory 的 kind 确定性生成。原因：

- 当前 Gateway/Runtime 只完整支持 Source fragment 输入，尚未完成 `DERIVED_OBJECT` 权威输入；
- ModelRun artifact 当前绑定 Knowledge artifact，不能正确 replay Action artifact；
- 先验证“认识经过确认后，用户是否愿意接受一个很小的行动”比增加第二条模型链更重要；
- 确定性模板能明确证明 Action 阶段 provider 调用次数为零。

首版只允许受审计的模板注册表，每次返回一个候选：

| Memory kind | 模板方向 | 约束 |
|---|---|---|
| `goal` | 写下或完成最小下一步 | 5–15 分钟；无 deadline |
| `value` | 做一次与该价值一致的小观察/尝试 | 5–15 分钟；允许立即停止 |
| `preference` | 安排一次低成本试验并记录感受 | 5–15 分钟；不作长期承诺 |

模板存 `template_id/version` 和非敏感参数，不复制 Memory/Source 正文。API 展示文本由模板和当前已授权 Memory 动态渲染；授权失效时不返回正文。

### 3.3 启动和运行必须 fail closed

新增显式 `candidate_insight_enabled`，不再用 `model_provider` 字符串暗示能力：

- disabled：不挂 Candidate 路由，不要求 provider/KMS 配置；
- enabled：所有依赖完整构造后才挂路由；任一依赖缺失时进程启动失败；
- production 禁止本地 AES key protector、fake provider 和测试 provider registry；
- `/health/capabilities` 只反映实际挂载的能力；
- 生产配置与注入 runtime 不一致时拒绝启动。

### 3.4 厂商选择是部署门，不阻塞前置实现

provider、对象存储和 KMS 尚未冻结。前四个实现批次使用注入式 contract 和 fake/recording adapter 完成；真实 canary 开始前必须冻结：

- provider/model/region、zero-retention、training use、timeout 和 request lookup 能力；
- object store bucket、版本语义、private ACL、删除一致性；
- KMS key、envelope format、AAD、轮换和历史 key 验证窗口。

不在代码里假装一个未经审查的真实 provider profile。

## 4. 按顺序实施

### 批次 A：Production Candidate Runtime Factory

新增唯一 composition root，例如：

```python
build_candidate_insight_runtime(
    *,
    settings,
    sessions,
    provider,
    source_plaintext_reader,
    model_run_keys,
    safety_keys,
) -> CandidateRuntimeBundle
```

固定依赖图：

```text
ProductionSessionFactory
Source plaintext reader → SourceConsentAuthority
approved ModelProvider → ModelGateway
ModelGateway + Source authority + one task → GovernedModelGateway
KnowledgeAuthorizationSnapshotAdapter
CandidateInsightMemorySafetyClassifier
CandidateInsightPersister
ModelRunFingerprintFactory
→ GovernedModelRuntime
```

实现要求：

- task type、consent purpose、output type、schema/prompt/pipeline version、region、retention、training use、sensitivity、成本和延迟预算全部由代码中的批准 profile 冻结；
- provider SDK 隐藏重试和 fallback 关闭，不开放 tools/functions；
- transport timeout 小于 runtime 总预算；原始请求、响应和异常正文不进入日志或数据库；
- ModelRun HMAC 与 Candidate Safety HMAC 使用不同 domain/key；增加 active key ID，先支持固定 `v1`，并记录后续轮换迁移；
- factory 创建的 provider/KMS/object/auth client 由 lifespan 关闭，外部注入资源不重复关闭；构造中途失败也清理已创建资源；
- readiness probe 只能检查技术连通性，不发送用户正文。

完成标准：enabled 配置绝不能静默启动成没有 Candidate API 的服务。

### 批次 B：托管明文读取边界与真实合成 canary

当前 Source authority 在数据库事务内同步解密，只适合本地 ciphertext。真实对象/KMS I/O 不得占用数据库连接，运行协议调整为：

```text
1. authorize refs 短事务：读取 object ref、版本、hash、consent 和 fences
2. 无 DB 事务：object GET + envelope decrypt，仅在内存持有明文
3. prepare 短事务：复验 exact refs/fences，创建 ModelRun receipt
4. dispatch 短事务：再次复验并 claim
5. 无 DB 事务：provider I/O
6. finalize 短事务：复验并原子落 artifact + succeeded
```

新增 provider-neutral ports：

- `EncryptedObjectStore`: version-exact put/get/head/delete；
- `EnvelopeKeyService`: wrap/unwrap data key，不暴露 master key；
- `ManagedSourcePlaintextReader`: 校验 bucket、object key、version、AAD、ciphertext tag 和权威 content hash；
- `CanaryRunner`: 只接受代码内合成内容。

真实 canary 必须完成：

```text
合成 plaintext
→ envelope encrypt
→ private object put
→ version-exact get
→ decrypt + AAD/hash 校验
→ Candidate ModelRun
→ durable Memory artifact
→ replay 同一 artifact 且 provider 不二次调用
→ delete object version
→ head/get 证明不可读
```

canary 失败不允许 readiness，且不得使用真实用户 Vault 或用户正文。

### 批次 C：Deletion Outbox 与 Maintenance Worker

修改 Source delete 的同一业务事务，使其同时写入：

- tombstone/fence；
- content-free `source.deleted` outbox event；
- 每个删除 sink 的 opaque job，只含 Vault、Source、revision/object technical IDs 和指纹。

首个真实 sink 只接对象存储，其余 sink 先以明确状态保留：database-derived、search、graph、cache、provider-retention、export、backup-expiry。

worker 使用独立 maintenance session factory，普通业务 role 仍看不到 tombstone。每个 job 遵循：

```text
claim lease
→ 读取 tombstone 与 exact object binding
→ 调用 sink delete
→ 通过 head/version check 验证
→ mark succeeded / retryable / unknown
```

关键规则：

- delete 必须幂等；对象已不存在视为成功；
- timeout 后结果不确定进入 `UNKNOWN`，不伪装失败或自动无限重试；
- worker 不读取或记录 Source plaintext；
- 普通 API 不能通过放宽 RLS 查询删除状态；
- provider-retention sink 在没有 provider 对账 API 前不能标记已擦除，只能记录等待自然过期或人工证明；
- backup 只承诺加密备份的生命周期过期，不宣称即时物理删除。

完成标准：合成对象从 Source DELETE 到 object store 验证不可读全链可重放、可恢复、可审计。

### 批次 D：Memory Verdict 幂等与 Action Eligibility

在进入 Action 前先修当前闭环的一处可靠性缺口：

- `POST /v1/memories/{memory_id}/verdicts` 增加必填 UUID `Idempotency-Key`；
- 新增 content-free append-only command receipt；
- 相同 key/相同请求返回原结果，相同 key/不同请求返回 409；
- replay 必须先于旧 `If-Match` 检查，否则响应丢失后的重试会误报 revision conflict。

新增窄的 `ActionEligibleMemoryAuthority`，在同一授权事务中返回不可伪造的 snapshot。只有同时满足以下条件才允许生成 Action：

- current derived version 与客户端 `If-Match` 精确一致；
- lifecycle 为 ACTIVE；
- 最新 decisive verdict 是用户对当前 derived version 的 `CONFIRM`；
- `CORRECT` 后的新版本必须再次确认，旧确认不能沿用；
- 无 reject/retract/governance suppression；
- 至少一个 exact、live、当前有 consent 的支持证据；
- membership、principal、Vault、相关 Source identity 和 fingerprint 全部当前有效。

用户主动请求微实验使用 passive/review 授权，不偷用 proactive permission。无关 Source 的新增不应让 action 自动失效；相关 Source 删除或撤权必须使读取/接受默认拒绝。

### 批次 E：可撤销微实验的持久化、Authority 与 API

#### 数据模型

1. `action_candidate`
   - Vault-scoped ID；
   - exact `memory_id + derived_object_id + memory_etag/verdict_id` lineage；
   - `template_id/version`、duration、generation、server fingerprint；
   - `proposed | accepted | skipped | superseded | revoked`；
   - safety policy/key ID/receipt identity；
   - `created_at/accepted_at/revoked_at`；
   - DB CHECK：`is_reversible=true`、duration 5–15、无 deadline/priority/external scope/payload。

2. `action_event`
   - append-only `proposed | accepted | skipped | revoked`；
   - actor、sequence、idempotency key HMAC、request HMAC、exact candidate generation；
   - 不保存 Memory/Source 正文。

3. `action_safety_receipt` 与 `action_authority_receipt`
   - content-free、HMAC/key ID、policy version、subject fingerprint、有效期和 reason code；
   - 不允许客户端铸造或直接提交 verdict/snapshot；
   - receipt/event 对业务 role append-only。

首版不提供自由文本 adjust。用户可以跳过，再回到已确认认识；避免引入“替换文本重新 Safety、旧凭证失效、generation supersede”的额外状态面。收集到真实需求后再增加完整 replacement 式 adjust，绝不原地改写。

#### 状态语义

```text
proposed → accepted → revoked
        ↘ skipped
```

- `accepted` 表示接受一个内部微实验，不触发外部执行；
- `skipped` 和 `revoked` 都是降能力操作，不因 consent 已撤回而阻止用户执行；只要求当前 membership 和 exact object binding；
- 相关 Source 删除/consent 撤回后，不再返回带上下文的展示文本，也不允许 accept，但必须允许 revoke；
- regrant 不会复活旧 action，用户需要重新生成；
- 同一 confirmed derived version 同时最多一个 live candidate。

#### Safety 与 Authority

- 对最终渲染的 exact action 做独立确定性 Action Safety 评估；不能复用 `CandidateInsightMemorySafetyClassifier`；
- fail/unknown/exception 一律不落 candidate；
- accept 时重验 current safety receipt、Action authority、Memory endorsement 和 policy；
- 使用服务端铸造的 interaction/session ID，不能信任客户端 header；
- `accept_experiment` 需要 production `ActionAuthorityPort`、可信时钟和 DB-backed single-use `EXPERIMENT_ACCEPTANCE` permit；
- revoke 不执行 connector，不创建 outbox，不需要普通行动许可。

#### API

```text
POST /v1/memories/{memory_id}/actions
  headers: Authorization, X-Vault-ID, If-Match, Idempotency-Key
  body: {}
  result: exactly one proposed reversible_experiment

GET /v1/actions/{action_id}
  result: current projection + ETag

POST /v1/actions/{action_id}/verdicts
  headers: If-Match, Idempotency-Key
  body: { verdict: accept | skip | revoke }
  result: current projection + ETag
```

所有响应 `Cache-Control: private, no-store`。跨 Vault、不可见对象和已撤权内容统一不可枚举 404；stale ETag、非法状态和幂等冲突使用稳定 409 problem code。

### 批次 F：完整闭环 canary 与上线门

在真实非 owner PostgreSQL runtime role 下跑一条合成闭环：

```text
Source create
→ Candidate generation + replay
→ Evidence excerpt
→ Memory confirm + replay
→ Action create + replay
→ Action accept
→ Action revoke + replay
→ Source delete
→ deletion worker
→ object version verified absent
```

只有这一条闭环通过才把相关 capability flag 打开。

## 5. 测试矩阵

| 类别 | 必须覆盖 |
|---|---|
| 配置/启动 | disabled；enabled 缺任一依赖；fake/local adapter 在 production；配置与实际路由不一致；secret 不出现在错误/repr/log |
| Provider runtime | 正常、replay、schema/tool 拒绝、timeout/cancel/transport unknown、dispatch 前撤权、provider I/O 时无 DB transaction |
| Object/KMS | wrong AAD/key/version/bucket、篡改 ciphertext、对象不存在、KMS timeout、delete 后 head/get、client 生命周期 |
| Deletion | outbox 与 tombstone 原子性、重复 delete、lease 竞争、retry/unknown、maintenance 权限、普通 role 不见 tombstone |
| Memory verdict | replay、同 key 异请求、响应丢失、并发 verdict、stale ETag、correct 后重新 confirm |
| Action authority | 未确认、reject/retract/snooze、stale evidence/consent、跨 Vault/principal、旧 generation/receipt、过期 policy |
| Action state | 恰好一个候选、accept/skip/revoke、重复命令、accept-vs-revoke 竞争、regrant 不复活 |
| Safety | blocked/unknown/error 零 candidate；交换 fingerprint/policy/principal 全拒绝；receipt 无正文 |
| 无副作用 | Action create/accept/revoke 前后 outbox/job/outbound_operation 不变，connector 和 provider 调用次数均为 0 |
| PostgreSQL | fresh upgrade→downgrade→upgrade、FORCE RLS、复合 Vault FK、append-only trigger、partial unique、连接复用 scope 清理 |
| 隐私 | DB、outbox/job、日志、trace、异常均无 Source plaintext、prompt、raw model output 或 provider error body |

## 6. 建议提交顺序

每个提交都保持可测试、可回滚，不把 schema、provider 和 API 混成一次大合并：

1. `fix(config): fail closed on incomplete candidate runtime`
2. `feat(ai): add production candidate runtime composition`
3. `refactor(ai): materialize managed source plaintext outside db transactions`
4. `feat(storage): add managed object and envelope key ports`
5. `test(canary): exercise synthetic object and model lifecycle`
6. `feat(deletion): enqueue source deletion outbox atomically`
7. `feat(worker): execute object deletion through maintenance scope`
8. `feat(memory): make verdict commands idempotent`
9. `feat(action): persist reversible experiment candidates and events`
10. `feat(action): add production action and safety authorities`
11. `feat(api): expose the single reversible experiment loop`
12. `test(postgres): verify the complete non-owner user canary`

每个 schema 提交都同步更新：Alembic、model registry、RLS/privilege/trigger 生成器、metadata parity、SQLite 领域测试和真实 PostgreSQL 测试。

## 7. 完成定义

本计划完成必须同时满足：

- production enabled 配置能构造并挂载真实 Candidate runtime；部分配置无法启动；
- 合成数据通过真实 provider、KMS 和对象存储 canary；
- Source 删除能驱动 durable worker，并验证真实对象不可读；
- Memory confirm/correct 具备安全幂等 replay；
- 只有 exact current、已确认且证据仍 live 的 Memory 能产生一个微实验；
- 用户可以接受、跳过和撤销，且这些操作没有外部副作用；
- 所有路径通过非 owner PostgreSQL、RLS、并发、迁移、隐私和故障注入测试；
- capability flag 与实际路由/依赖一致；
- 未实现的厂商能力和物理删除边界被诚实记录，不作过度承诺。

## 8. 明确延后

- 模型生成个性化 Action、`DERIVED_OBJECT` 模型输入和 Action ModelRun artifact；
- Action 自由文本调整、完成率、连续打卡、通知和主动推送；
- Todo、Calendar、消息、支付等外部 connector；
- 多 provider、自动 fallback、自由 prompt、用户选模型；
- 回忆录章节、人生主线、长期模式自动解释；
- 诊断、治疗建议、人格评分或用行动完成率评价用户。

## 9. 执行起点

可以立即开始且不依赖厂商选择的是批次 A 的 fail-closed Settings、runtime factory、生命周期与 fake composition 测试。真实 provider/KMS/object canary 到来前，代码保持 capability disabled，不用临时实现冒充 production。
