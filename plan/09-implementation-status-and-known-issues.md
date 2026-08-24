# 实现状态、已知问题与下一阶段交接

更新日期：2026-08-24

## 0. 2026-08-24 收口增量

- 已加入 `Principal` 与 RLS-governed `VaultMembership`，运行角色对 membership 只有 `SELECT`，不能自助授权、改角色或撤销。
- 已实现 OIDC token introspection adapter；校验 active、issuer、audience、`exp`/`nbf` 与内部 `principal_id` UUID，生产环境认证缺失或配置不完整时拒绝启动。
- 已实现唯一 `ProductionSessionFactory`：验证 token → 设置请求 Vault 的事务级 RLS scope → 同事务读取有效 membership → 才向业务层交付 session。
- 已新增 disposable PostgreSQL staging 演练：`upgrade → downgrade → upgrade`，并以非 owner `life_coach_app` 验证无 scope 零可见、跨 Vault 隔离、membership 只读。
- 2026-08-24 在本地 PostgreSQL 16 一次性容器中实跑上述两项集成测试，结果 `2 passed`；容器随后已删除。
- 已加入 `GovernedModelGateway` 第一阶段：调用方只能选择服务器注册任务和 Source fragment；provider、region、retention、sensitivity、consent snapshot、policy/source fence 与 `ModelInputRef` 均由权威 adapter 生成。
- 已实现 `SourceConsentAuthority`、`KnowledgeEvidenceAuthorityAdapter` 与 `KnowledgeAuthorizationSnapshotAdapter`：仅当前 revision 的 live Source 可被解密读取，plaintext 必须匹配 SHA-256，Knowledge evidence span 必须由 Source 重新计算，缺少或过期 consent 默认拒绝。
- 已加入架构回归：`src/life_coach` 中只有 provider kernel 可以调用 `provider.complete()`。
- 已实现 content-free `ModelRun`/`ModelRunInput` 权威收据、幂等 prepare、dispatch generation CAS、精确终态写入和过期 dispatch → `unknown`；输入/请求/任务指纹均为 Vault-scoped HMAC。
- 已新增 Alembic revision `d4c8a1e7f302`，在一次性 PostgreSQL 16 中再次实跑 `upgrade → downgrade → upgrade`，并以非 owner `life_coach_app` 验证 ModelRun FORCE RLS、跨 Vault 拒绝、receipt 不可删除及 input 不可变。
- 已实现 `GovernedModelRuntime`：prepare/dispatch/finalize 使用独立短事务，provider I/O 不持有数据库连接；membership generation、Source/Consent 与 Vault fences 在关键边界复验。
- 已实现稳定失败语义和原子结果 sink：timeout/cancel/provider 不确定性进入 `unknown`，确定性输出拒绝进入 `failed`；结果落库与 `succeeded` CAS 同事务，stale ticket 或持久化异常整体回滚。
- 已把认证后的 Source API 挂入真实 composition root：`POST/GET/PATCH/DELETE /v1/entries` 全部经过 Bearer authentication、`X-Vault-ID` membership 与同一 request transaction；提交失败不会提前返回成功。
- 已新增 content-free、Vault-scoped、append-only 的 Source command receipt 与 Alembic head `7e2c1f9a6b40`；写操作强制 `Idempotency-Key`，正文与原始 key 不进入 receipt，冲突与重放均有安全契约。
- Source 正文在开发/合成 canary 中使用带随机 nonce、Vault 派生密钥和权威 AAD 的 AES-256-GCM；生产拒绝从配置构造本地 protector，只保留托管 protector 注入边界。
- 已用独立非 owner LOGIN runtime role 实跑完整 Source HTTP 闭环：创建、幂等重放、读取解密、追加修订、删除、删除重放和 tombstone 后 404；同时再次完成 PostgreSQL 16 `upgrade → downgrade → upgrade`。
- 真实演练发现并修复了业务角色 tombstone 的 RLS 冲突：Source 隔离、派生内容清空与双 fence 推进现在由仅接受当前 Vault scope 的窄 `SECURITY DEFINER` 函数原子完成，未放宽普通角色对 tombstone 的可见性。

## 1. 本轮交付边界

本轮目标是形成一个可运行、可测试、可继续扩展的后端基线，而不是一次性完成生产产品。当前代码已经覆盖：

- Vault 租户边界、Source 原始记录与不可变修订；
- append-only Consent、策略版本与来源代际 fence；
- 证据化 Life Model、双时态 claim、用户 verdict/correction；
- AI 记忆门、混合检索、Context Pack 与模型供应商边界；
- Safety、Reflection、Action 的纯领域规则；
- PostgreSQL outbox、Job lease、重试、UNKNOWN reconciliation 与外部副作用账本；
- PostgreSQL FORCE RLS、live Source ancestry、append-only trigger、单调 tombstone 与非 owner 运行角色；
- 模块级和跨模块自动化测试。

这是一套“领域内核 + 信任边界 + 持久化骨架”。它还不是完整可上线应用。

## 2. 合并原则

- 默认拒绝优先于智能程度；用户原文、用户纠正和撤销优先于模型推断。
- 原始记录、推断、行动建议和已执行副作用必须是不同状态。
- 外部调用必须经过 consent、provider policy、source generation、policy epoch 和 lease generation 检查。
- 本轮无法完成但不会破坏数据正确性的内容，记录为后续工作，不阻塞基线合并。

## 3. 已知问题

### P0：上线前必须完成

1. **身份开通与撤销运营路径尚未接入。** 请求期 OIDC → principal → membership 已接线，但 principal 的 keyed subject fingerprint 生成、首次 Vault/owner membership 原子开通、管理员撤销、IdP claim 配置与密钥轮换仍需部署级实现。不得允许业务 DTO 自报 principal，`principal_id` 只能来自已验证 token claim。
2. **Alembic 仍需目标基础设施演练。** disposable PostgreSQL 16 上的自动化演练已经完成；目标 staging 仍需使用真正的非 superuser migration login、独立 runtime login、基础设施托管角色与备份恢复策略再演练一次。当前自动化以 migration owner 登录后 `SET ROLE life_coach_app` 验证非 owner 数据路径。
3. **隐私删除链尚未端到端落地。** Source tombstone 和删除计划已经存在，但 object store、缓存、搜索、图、供应商留存、导出文件、备份过期和失败补偿仍需 durable outbox worker 与删除 canary。
4. **真实加密与密钥管理未接入。** 当前只接受 ciphertext/对象引用并避免宣称假 E2EE；生产需要 KMS/envelope encryption、密钥轮换、对象存储 ACL、备份加密与恢复演练。
5. **危机安全流程需要运营配置。** 代码只提供非诊断、安全路由和输出门；地区化危机资源、人工升级、值班流程、法律文案和临床审阅必须在上线前完成。

### P1：MVP 内应完成

1. **权威适配器仍未全部统一接线。** Source/Consent → Knowledge/AI 的第一阶段 adapter 已落地；Safety receipt、Action single-use authorization、Jobs exact authorization 以及端到端业务 composition root 仍需接线。
2. **Model Gateway 仍缺真实供应商、真实密钥设施与业务结果适配器。** 唯一受治理出口、短事务运行时、provider-region pair、数据处理策略、HMAC receipt、调用 timeout/cancel 和原子结果 sink 已落地；仍需实现真实 provider adapter、KMS/object-store plaintext reader、供应商策略 canary，以及首个冻结输出协议对应的 Knowledge candidate/evidence persister。当前可信 registry 仍是测试技术标识，不代表真实供应商已获批准。
3. **业务 API 仍不完整。** Source entry 已具备认证依赖、统一 Problem Details、敏感错误脱敏、幂等账本、游标分页和真实 PostgreSQL composition；Memory review/verdict、candidate insight、Action API、限流与审计事件仍未挂入同一生产应用。
4. **删除后的幂等查询需要 maintenance 边界。** 普通业务角色按设计看不到 tombstone；删除状态查询、重试和擦除只能通过受限 maintenance repository，不能放宽普通 RLS。
5. **外部连接器尚未实现。** 日历、Todo、邮件等只应在用户逐项确认后执行；需实现“先持久 CAS 消费授权，再调用 connector”，以及 UNKNOWN 对账和人工处理后台。
6. **回忆录与人生主线尚未端到端实现。** 数据模型支持 evidence、bitemporal claim、narrative candidate 与用户 verdict，但章节规划、引用覆盖、冲突展示、版本比较和导出仍是后续应用层工作。
7. **心理学评估需要产品化验证。** 反思提示应继续采用 MI/SDT/ACT/CBT/叙事身份的低推断表达，并通过用户研究验证节奏、措辞和再呈现策略；不得把产品输出当诊断。

### P2：可在 MVP 后优化

1. 检索与 Life Model 的大规模性能、索引选择、归档和冷热分层。
2. 多语言语义安全、方言、错别字和混合语言的持续 adversarial eval。
3. 可观测性：SLO、队列积压、stale fence、RLS denial、删除延迟和供应商策略拒绝指标。
4. 数据导入、批量修订、离线设备同步与冲突解决。
5. 管理后台、隐私中心、数据导出/可携带性和审计报告 UI。

### 本轮审计确认、但主动延后的技术债

下表记录的是已经定位到具体边界的问题，不代表当前基线已解决。它们应进入下一轮的回归测试与实现清单。

| 优先级 | 问题 | 当前影响与收敛方向 |
|---|---|---|
| P1 | Consent 在 PostgreSQL trigger 分配新 `policy_epoch` 后，只刷新了 `ConsentRecord`，同一 Session identity map 中的 `Vault` 仍可能保留旧 epoch | 同一事务后续 projection/read 可能使用旧 fence；写入后应刷新或重新读取权威 Vault snapshot，并增加真实 PostgreSQL 同事务回归 |
| P1 | `UserConsentCommand` 的过期与 replay 快速检查发生在等待 Vault 行锁之前 | 锁等待可能跨过 `expires_at`，并发 replay 可能落为裸 `IntegrityError`；获得锁后需再次校验时间与 interaction，并统一映射领域错误 |
| P1 | SEARCH revoke 后的 projection 被 tombstone，而当前唯一键、RLS 可见性与 regrant 的“复用旧行”语义不一致 | 真实 PostgreSQL 下旧行对 business role 不可见，新建又会撞唯一键；需选择 append-only 新 projection 或受限 maintenance 复用，并把 consent lineage 明确建模 |
| P1 | 数据库中的 object key `CHECK` 比应用层 canonical validator 更宽松 | 绕过 ORM/Core binder 的直接 SQL 仍可写入空 suffix、空格、反斜杠或编码变体；迁移中应加入等价的 PostgreSQL 校验函数/约束 |
| P1 | Knowledge、AI、Safety、Action 的权威 port 目前主要由领域接口和测试 fake 覆盖 | composition root 尚未把 Source/Consent/fence/receipt 的持久化适配器统一接线；生产路径必须禁止调用方自铸 verdict、snapshot 或 confirmation |
| P1 | Safety/Action 的 Authority、TrustedClock 与单次 permit 目前是纯领域端口，签发和验证能力仍由同一聚合协议表达 | 生产 DI 需拆成最小权限 signer/verifier/consumer，并将 receipt、revocation、expiry 与 consumption 写入权威持久层 |
| P1 | 外部 connector、模型 provider 与删除 sink 尚无真实实现 | 领域层已定义 gate、lease、UNKNOWN 与 fence，但仍需用真实适配器验证“每次 I/O 前重验、先消费单次许可、后副作用、失败可对账” |
| P1 | `alembic check` 对当前自动生成的 CHECK 名称仍会报告 drift | PostgreSQL 会截断超过 63 字节的名称，反射式 enum CHECK 也会被 compare 插件误判；迁移已实跑通过，但下一轮应缩短模型约束名并配置语义化 compare hook |
| P2 | Safety 的正则检测仍可能漏掉部分中英混排、谐音和规避表达 | 当前语义 verifier 未配置时默认拒绝高风险输出；上线前仍需中文 adversarial corpus、版本化语义分类器与人工复核抽样 |
| P2 | PostgreSQL business role 看不到 tombstone，删除幂等查询与恢复审计缺少专用 repository | 保持普通 RLS 默认拒绝；为删除 worker 建立最小权限 maintenance API，而不是扩大业务角色可见范围 |
| P1 | `ModelResultPersister` 目前只有事务端口与测试实现，没有首个任务的 Knowledge candidate/evidence 生产 adapter | 在唯一用户闭环中先冻结输出 schema；adapter 只能引用本次 authorized fragments，强制覆盖 `model_run_id`，不得接受模型自报 Source/run 身份 |
| P1 | Knowledge authorization port 不携带 Source IDs | 当前 adapter 对 source-specific grant 保守拒绝，仅接受 vault-wide consent；后续应把已验证 evidence Source IDs 绑定进 authorization request，而不是扩大授权 |
| P1 | Source plaintext reader 只有同步最小权限 Protocol，没有 KMS/object-store 实现 | 当前 prepare 事务内读取 plaintext 只适合本地 adapter；真实对象/KMS I/O 必须设计成不占用数据库连接的分段读取，并做 AAD、对象版本、密钥轮换与完整性 canary |
| P1 | ModelRun HMAC 配置只有单一 secret，没有持久化 key ID/keyring | 密钥轮换会改变同一输入的指纹并影响历史幂等判定；真实 provider 前冻结 key ID、旧 key 校验窗口和重签边界 |
| P1 | ModelRun repository 尚无面向幂等 replay 的终态 projection/artifact lookup | 当前重复成功请求不会再次调用 provider，但 application runtime 只返回 dispatch conflict；首个闭环需按 `model_run_id` 读取 durable candidate，UNKNOWN 重试必须由用户显式产生新幂等键 |
| P1 | Knowledge 中既有 nullable `model_run_id` 尚未建立 Vault 复合外键 | 新派生路径先强制引用真实 receipt；盘点历史孤立值后再分步加 FK，禁止伪造历史运行补录 |
| P1 | Source 的生产 protector 目前只有同步注入协议 | 本地 AEAD 不做生产声明；真实 KMS/Object Store 网络 I/O 不能占用请求数据库事务，需拆成短事务预留对象身份 → 无连接加密/上传 → 短事务 finalize，并处理对象孤儿与 UNKNOWN |
| P1 | Source title 尚无受保护存储 | API 当前对非空 title 默认拒绝，避免把敏感标题明文落库；应在冻结搜索/展示需求后复用受保护内容边界或单独建立加密 envelope |
| P1 | Source HMAC 与 cursor 只有单一 secret、无 key ID | 轮换前需加入 keyring/version；旧 key 仅用于验证历史 receipt/cursor，新 key 用于签发，避免重放语义在轮换时断裂 |

## 4. 下一阶段建议顺序

1. [x] 接入认证、principal-vault membership 与 production session factory（请求期路径完成；部署级 provisioning 见 P0）。
2. [x] 完成 disposable Alembic staging 演练和非 owner PostgreSQL 集成测试（目标基础设施演练见 P0）。
3. [ ] 实现唯一 Model Gateway，并接入 Knowledge/AI 的权威 Source 与 consent snapshot adapter（唯一 kernel、authority、receipt 和短事务编排已完成；真实 provider、KMS/object-store 和首个业务结果 adapter 待完成）。
4. [ ] 实现 deletion/outbox worker 和一个真实对象存储 canary。
5. [ ] 只选择一个完整用户闭环上线：记录一条碎片 → 生成一个带证据的候选认识 → 用户确认/纠正 → 生成一个可撤销的小行动。
6. [ ] 闭环稳定后，再做回忆录章节、人生主线和外部 Todo/Calendar。

## 5. 完成定义

下一阶段的功能只有同时满足以下条件才算完成：

- 缺少 consent、过期授权、来源删除或 fence 变化时默认拒绝；
- 用户能查看来源、纠正、拒绝、撤销并删除；
- 每个推断可追溯到具体 Source span，且不会把推断伪装成事实；
- 外部副作用可证明由一次未过期、未撤销、未消费的用户授权触发；
- 在真实 PostgreSQL 非 owner 角色下通过跨 vault、连接复用、RLS、trigger 和迁移测试；
- 失败日志、异常、trace 和任务 payload 不包含用户正文。
