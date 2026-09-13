# Morrow 长期记忆与 AI 对话：后端技术实现书

版本：1.0，2026-09-10
状态：基于实际代码审查的实施设计；新增表、接口和流程均为拟实现内容。
代码基线：HEAD `3de28290363b4a06b718140770402b5999bd545d` 加当前工作区；工作区包含尚未提交的桌面字体改动。
交付对象：后端开发、桌面端开发、测试与发布负责人。

## 0. 决策摘要

产品以日记持续积累材料，AI 对话按授权使用长期记忆。长期记忆包括原始经历、持续中的事情、用户认可的认识及其纠正和变化；不能退化成一份不断追加的用户摘要。

**保留现有 Python/FastAPI + PostgreSQL 模块化单体、Windows 内置运行时、Source → Derived → Evidence → Verdict 治理链、数据库任务及 ModelRun 调用收据。补齐实际摄取、检索、对话和依赖失效链路。**

最优先的五项改造：

1. 建立日记增量索引任务，接通已经存在但未进入产品链路的检索和 ContextPack 逻辑。
2. 分离索引构建版本与请求授权快照，避免每新增日记使全部历史索引失效。
3. 新增持久化对话、轮次、上下文清单和答案引用，接入现有受治理模型运行时。
4. 复用 ClaimVersion / UserVerdict，补齐持续事项和事件材料；模型回答不能作为用户事实再次摄取。
5. 对编辑、删除、撤权和模型切换建立统一依赖失效流程，覆盖新答案、摘要和检索缓存。

首版不引入独立向量数据库、图数据库、Redis/Celery 或多智能体框架；不微调用户专属模型；不承诺真正流式输出。先交付可靠的异步对话，再决定是否扩展流式协议。

本文优先于 [28 产品设计稿](./28-morrow-product-design-v1.md) 中“探索为中心”的旧入口描述：最新产品方向为日记与对话共享记忆。本文不修改发行版行为，也不授权自动迁移用户旧版本数据。

## 1. 审查范围与证据等级

### 1.1 已检查的实际模块

| 范围 | 代码入口 | 审查结论 |
| --- | --- | --- |
| 应用装配 | [api/app.py](../src/life_coach/api/app.py) | FastAPI 工厂、受保护路由、候选任务 worker 随 lifespan 启停 |
| 日记写入 | [application/source_entries.py](../src/life_coach/application/source_entries.py) | 加密、幂等、修订、分页、删除；当前每篇修订写一个全文 fragment |
| 来源及索引 | [sources/models.py](../src/life_coach/modules/sources/models.py)、[sources/service.py](../src/life_coach/modules/sources/service.py) | 已有 SearchProjection 与维护服务，缺生产摄取与检索装配 |
| 认识与裁定 | [knowledge/models.py](../src/life_coach/modules/knowledge/models.py)、[service.py](../src/life_coach/modules/knowledge/service.py)、[reducer.py](../src/life_coach/modules/knowledge/reducer.py) | 已有版本、双时间字段、证据、纠正、抑制，不应另建一个无治理的 profile 主库 |
| AI 检索与上下文 | [ai/retrieval.py](../src/life_coach/ai/retrieval.py)、[ai/context.py](../src/life_coach/ai/context.py) | 已有纯函数混合检索与 ContextPack；搜索调用点未发现实际应用路径调用 |
| 时间与实体 | [ai/temporal.py](../src/life_coach/ai/temporal.py)、[ai/entities.py](../src/life_coach/ai/entities.py) | 时间解析与实体候选纯逻辑；不是完整的持久化事件/实体系统 |
| 单篇分析 | [candidate_insight_entry.py](../src/life_coach/application/candidate_insight_entry.py) | 只解析目标 entry 当前修订的 fragment IDs，无跨日记检索 |
| 模型治理 | [model_gateway.py](../src/life_coach/application/model_gateway.py)、[model_runtime.py](../src/life_coach/application/model_runtime.py) | 来源授权、受控任务、三段短事务、收据、输出落库 |
| Provider | [ai/provider.py](../src/life_coach/ai/provider.py)、[ai/stepfun.py](../src/life_coach/ai/stepfun.py)、[ai/compatible.py](../src/life_coach/ai/compatible.py) | 同步结构化 JSON 请求，无现成聊天流式接口，任务提示混在适配器中 |
| 队列 | [jobs/repository.py](../src/life_coach/jobs/repository.py)、[candidate_insight_jobs.py](../src/life_coach/application/candidate_insight_jobs.py) | 通用 job/outbox 基础存在，但实际 worker/dispatcher 为候选分析专用 |
| 授权与隔离 | [consent/models.py](../src/life_coach/modules/consent/models.py)、[platform/auth.py](../src/life_coach/platform/auth.py)、[postgres_security.py](../src/life_coach/platform/postgres_security.py) | purpose consent、Vault RLS、版本栅栏、来源隔离函数 |
| 桌面发行 | [managed-runtime.cjs](../apps/desktop/electron/managed-runtime.cjs)、[preload.cjs](../apps/desktop/electron/preload.cjs)、[seed_local.py](../scripts/seed_local.py) | 主进程代理 API；模型切换关联运行时和授权；非浏览器直连服务器部署 |

### 1.2 本次验证

运行以下现有测试，结果 **117 passed**；pytest 缓存目录写入有权限警告，未影响测试断言。

```powershell
.venv/Scripts/python.exe -m pytest tests/ai/test_retrieval.py tests/ai/test_memory.py tests/sources/test_source_service.py tests/jobs/test_state_machine_and_retry.py
```

这仅验证所列现有测试。本次没有验证真实供应商效果、执行新迁移、完成跨日记对话或跑全量发行验收。以下性能数字均为建议预算/验收目标，不是测量结果。

## 2. 当前架构的真实状态

### 2.1 当前业务路径

```text
Electron Renderer
  -> preload apiRequest -> Electron 主进程 -> 本机 FastAPI
  -> 认证 + Vault 事务
  -> SourceDocument -> SourceRevision -> SourceFragment

用户发起单篇分析
  -> entry 当前修订的 fragment IDs
  -> 候选任务 worker（启用异步时）
  -> GovernedModelRuntime
  -> SourceConsentAuthority + Provider
  -> CandidateInsight 校验 / Knowledge 写入
  -> MemoryClaim / ClaimVersion / EvidenceLink
  -> UserVerdict -> 当前认识
```

动作与叙述已有各自的 context authority 与结果 persister，可参考它们新增对话任务，不应在 chat router 中直接调用供应商。

### 2.2 必须澄清的差距

| 已有基础 | 尚不能据此宣称的能力 | 改造要求 |
| --- | --- | --- |
| pyproject 依赖 pgvector | 已上线向量数据库索引 | 当前 embedding 为 JSON；审查到初始迁移创建 btree_gist，未发现 vector 扩展部署链 |
| SearchProjection 服务 | 保存日记就能检索历史 | 写入链没有索引任务装配，要接通实际任务和查询适配器 |
| hybrid_retrieve / build_context_pack | AI 正在使用长期记忆 | 目前只有定义/导出，应用层未接通；新增授权 repository 与 authority |
| ClaimVersion 双时间字段 | 变化理解已自动回流到聊天 | 对话尚不存在，要建立选取有效版本和纠正优先规则 |
| SourceType.CONVERSATION | 已有会话和消息系统 | 来源类型不等于 session/turn/messages，需要专门生命周期 |
| JobQueue 多种队列 | 通用生产 worker 已运行 | 现有运行 worker 使用候选分析专用领取函数 |
| 删除来源与清索引 | 所有未来生成内容会自动遗忘 | 需把摘要、对话答案和记忆材料依赖纳入失效链 |

### 2.3 三个必须先处理的耦合点

**索引代际耦合。** `list_search_projections` 要求行的 `source_generation`、`policy_epoch` 与 Vault 当前值相同；日记新增、修订会推进 generation。将旧索引直接接入长期记忆会产生大面积失效。纯检索契约也检查 generation，不能只修改 SQL。

**原文身份与聊天历史耦合。** `SourceConsentAuthority` 要求至少一个当前原文 fragment；当前模型输入种类只有 source revision、source fragment、derived object。对话要区分用户消息、助手消息和当前请求，不能将整段含 AI 回答的聊天当成 Source。

**Provider 与任务提示耦合。** StepFun 适配器的 `_messages` 内部按 task_type 选择任务提示。新增更多任务前，应把可信任务提示移至注册层，供应商仅负责协议封装。

## 3. 学术依据及适用范围

本节提炼设计依据，不将论文实验直接等同于 Morrow 产品效果。

| 研究 | 支持的具体设计 | 不应推导出的结论 |
| --- | --- | --- |
| [MemGPT](https://arxiv.org/abs/2310.08560)，2023/2024 | 模型上下文与外部长期存储分层、按需调入材料 | 不代表能无限准确记忆，也不要求直接采用其框架 |
| [LongMemEval](https://arxiv.org/abs/2410.10813)，ICLR 2025 | 索引—检索—阅读分层；评测提取、跨会话、时间、更新和拒答 | 不代表基准问答高分就有高质量自我理解 |
| [LoCoMo](https://aclanthology.org/2024.acl-long.747/)，ACL 2024 | 用跨会话事件和长期问答评测历史连接能力 | 数据集不是自然使用 Morrow 的真实日记样本 |
| [MindScape](https://arxiv.org/abs/2404.00487)，2024 | 根据个人背景生成情境化日记提示的早期交互探索 | 不能承诺临床或长期收益，也不要求采集行为传感器数据 |

工程选择：借鉴研究的问题分解与评测方法，自有 Source/Knowledge 作为权威存储。外部记忆框架最多作为离线对照实现，不能绕过证据、授权和删除机制成为第二个主库。

## 4. 目标模块边界

```text
日记保存 ──> Source + 同事务任务 ──> 本地词法索引
                                  └─> 授权后提取事件 / 候选认识

日记 / 用户对话原文 ───────────────────> 权威材料
已确认认识 + 纠正 + 持续事项 ───────────> 可检索派生知识

对话请求 -> 会话轮次 -> 授权检索 -> ContextPack + ContextManifest
         -> GovernedModelRuntime -> 供应商 API
         -> 引用校验 + 依赖再验证 -> 答案 / 记忆更新候选

修改 / 删除 / 撤权 -> 即时阻断读取与提交 -> 依赖清理任务
```

拟新增模块：

| 目录 | 职责 |
| --- | --- |
| `modules/conversations/` | 会话、消息、轮次、答案版本和状态 |
| `modules/memory_context/` | 上下文选择、清单及输入依赖；不替代 Knowledge |
| `modules/episodes/` | 事件材料、持续事项及其关系；第二阶段接入 |
| `application/source_indexing.py` | 本地增量索引、修复与重建 |
| `application/memory_ingestion.py` | 授权提取和现有 Knowledge 写入协调 |
| `application/memory_retrieval.py` | 数据库取候选、纯检索、回源与预算 |
| `application/conversation_generation.py` | 对话任务定义、context authority、结果落库 |
| `application/dependency_invalidation.py` | 来源变更、撤权与删除传播 |

新增 ORM 模块必须加入 `platform/model_registry.py`、Alembic、RLS 与权限安装，不能只创建业务表。

## 5. 记忆数据模型

### 5.1 复用现有权威表

| 表 | 继续承担的职责 | 需要补充的行为 |
| --- | --- | --- |
| source_document / revision / fragment | 用户材料及不可变版本、原话证据 | 新增显式排除索引策略；聊天用户消息单独关联来源 |
| memory_claim / claim_version | 可变认识及版本 | 约定 structured_payload 中条件、适用背景；按有效时间选版本 |
| evidence_link | 精确原文引用及支持/反例/上下文关系 | 对摄取、合并建议持续回源；禁止仅凭摘要做证据 |
| user_verdict / memory_suppression | 确认、纠正、拒绝及同源抑制 | 对话查询读取相关最新纠正；保留 interpretation_error / life_stage_change 区别 |
| search_projection | 可重建的索引 | 分离内容版本和请求快照，增加检索范围与当前修订约束 |
| job / outbox_event / model_run | 持久任务与调用收据 | 注册新任务类型、结果引用、上下文依赖 |

不把“近期心情”“一句担忧”都升级为 MemoryClaim。它们首先是经历材料；现有 `decide_memory` 对短期情绪等的 episodic-only 设计应保留。

### 5.2 会话与消息：第一阶段新增

以下字段为逻辑规格，实施时采用现有 UUID、VaultScopedMixin、UTC 与复合外键风格。

**conversation_session**

- `id, vault_id, created_at, updated_at, deleted_at`。
- `title_ciphertext`：标题可能泄露内容，按新内容加密策略处理。
- `history_mode = saved | temporary`；第一阶段仅 saved，temporary 不在 capabilities 宣称。
- `revision`：乐观并发控制；`status = active | archived`。
- `default_material_scope = current_session | selected | history`，`allow_memory_extraction` 默认 false。
- 当前模型配置只是运行偏好，不把某供应商 session ID 作为主身份。

**conversation_message**

- `id, vault_id, session_id, sequence_no, role, created_at, deleted_at`。
- `role = user | assistant`；system 指令来自代码注册，不落成可检索个人消息。
- `content_ciphertext`：user 消息正文由关联 Source 保存，避免双份正文；assistant 正文存在此字段。
- `source_document_id, source_revision_id`：仅 user 允许且必须有；assistant 必须为空。
- `turn_id, status = pending | ready | failed | invalidated`。
- 唯一约束 `(vault_id, session_id, sequence_no)`；复合 FK 保证不跨 Vault。
- user 消息编辑在首版以“新增纠正消息”实现，不悄悄重写后续对话历史。

**conversation_turn**

- `id, vault_id, session_id, user_message_id, parent_assistant_message_id`。
- `status = queued | preparing | generating | validating | succeeded | failed | canceled | invalidated | outcome_unknown`。
- `job_id, model_run_id, answer_message_id`：可空，按状态约束。
- `session_revision, context_manifest_id, provider_activation_id, request_hash, idempotency_key_hash`。
- `safe_error_code, created_at, finished_at`；不持久化供应商原始异常体。
- 同一 session 默认只允许一个非终态 turn；重复请求返回同一个 turn。
- 重新生成产生新 turn 和 answer，不覆盖旧答案；旧答案标记非当前分支，不参与默认上下文。

第一阶段每条 user 消息创建 `SourceType.CONVERSATION` 的独立 SourceDocument，复用加密、版本、引用和来源授权；列表默认过滤，不能让聊天消息淹没日记页。普通会话原文默认不进入跨会话索引，需单独开启或显式“记住这点”。

### 5.3 上下文与答案依赖：第一阶段新增

**context_manifest**：`id, vault_id, session_id, turn_id, created_at, expires_at, retrieval_version, scope_revision, policy_epoch, source_generation, provider_activation_id, context_fingerprint, coverage_json, revoked_at`。

**context_dependency**：`id, vault_id, manifest_id, kind, source_document_id, source_revision_id, source_fragment_id, derived_object_id, message_id, content_fingerprint, ordinal, use_kind`。

- `kind` 限定 source / claim / assistant_message；字段组合 CHECK 每种身份的形状。
- source 和 claim 必须有相应复合 FK；claim 同时展开到底层 source 依赖。
- assistant_message 只是会话连续性材料，不能产生生活事实证据；其 source 依赖必须递归展开，首版限制深度并去重，失败时不带入该消息。
- `coverage_json` 只保留数量、检索是否完整、是否截断等技术信息，不保存正文和心理判断。
- manifest 是“这次用了什么”的收据，不是上传授权。使用前仍读取当前权威策略。
- manifest 的有效期约束生成，不妨碍之后展示历史答案；历史阅读必须检查当前依赖状态。

**answer_citation**：`id, vault_id, answer_message_id, citation_label, source_document_id, source_revision_id, source_fragment_id, quote_start, quote_end, quote_hash, relation, status`。答案不得只保存裸链接或模型自造来源号。

**derived_dependency**：记录需主动失效的目标与其输入（包括事件材料、事项摘要、答案）。第一阶段可通过受约束的 source、claim、message 可空列表示目标和输入，使用 CHECK + 复合 FK；避免不可验证的任意 `type/id` 多态字符串。新增目标类型必须同步扩展约束和清理处理器。

### 5.4 事件与持续事项：第二阶段新增

**episode_record**：`id, vault_id, source_revision_id, extraction_run_id, summary_ciphertext, event_start, event_end, time_precision, original_time_expression_ciphertext, capture_timezone, attribution, status, extractor_version`。

- 每条有 source fragment 锚点与 offset；建议使用专属 episode_evidence 表，不能强塞到只支持 DerivedObject 的旧 EvidenceLink。
- 摘要只是检索与展示辅助，回答需回源。
- `status = proposed | accepted | excluded | stale`；自动提取默认 proposed，可用于标明未确认的材料导航，不能作为已确认个人认识。

**ongoing_matter**：`id, vault_id, title_ciphertext, status, revision, created_at, updated_at`。

**matter_episode_link**：`vault_id, matter_id, episode_id, state, origin, user_verdict_reference`；模型建议关系与用户确认关系分开。错误关联可以解除而不删除原日记。

首版不建设通用人物知识图谱。利用当前 `resolve_entity_candidates` 作为保守候选工具，正式实体表和身份验证器在确有跨人物检索需求时再实施；“老板”“他”等不能自动合并。

### 5.5 加密与删除边界

新正文使用独立 `PrivateContentProtector` 端口，复用现有 AES-GCM 实现模式，AAD 绑定 Vault、表用途、对象与版本。不要直接让对话密文冒充 SourceRevision 密文。旧 ClaimVersion.canonical_text、索引词和向量目前是可读派生数据，本文不宣称全库加密。

删除需区分用户内容清除和内容无关审计保留。现有 UserVerdict 含 correction_text / reason 且限制更新删除：全库彻底遗忘需另加内容侧表/加密载荷迁移和受限维护清除通道，不能删除界面一行就宣称所有副本消失。首版普通来源删除遵循第 13 节，完整物理擦除边界在 API 返回中区分。

## 6. 增量摄取与索引

### 6.1 保存事务

写日记的同一事务内完成 Source、命令收据和 `source.index.v1` 任务；任务 payload 继续只用现有安全字段 `vault_id/resource_id/pipeline_version`，resource_id 指向独立 processing_request。具体修订、授权和子任务状态放请求表，不能把日记正文放 job/outbox JSON。

用户未开启 AI 时也可保存；本地搜索索引只在 SEARCH 授权成立时构建。开启自动 AI 整理后才追加 `memory.extract.v1`。历史补建是显式操作，先展示范围及预计调用数量，不能升级即全库收费重算。

**processing_request**：`id, vault_id, source_revision_id, operation, pipeline_version, policy_binding, status, requested_by, created_at`；唯一键覆盖 Vault/修订/操作/版本/授权激活。原文相同但不同事件日期的日记不可按 content_hash 合并成一条。

### 6.2 文本切块

保留现有全文 SourceFragment，首版新增 `search_chunk` 派生子块，不改写既有 fragment 身份或 offset。块包含 parent_fragment_id、原文字符起止、chunker_version、原文 hash；无需额外正文副本。

建议初始按段落/句界切分，目标 500–900 个 Unicode 字符，重叠不超过 120，长句硬切。它们是可调参数。引用偏移以 Python Unicode 字符为准；前端 JavaScript UTF-16 高亮需要显式转换并覆盖 emoji 测试。

已有加密 reader 根据对象身份解密，切块后仍回读 parent fragment，再截取；模型返回块内 quote offset 由服务端换算成 parent fragment offset 后验证。

### 6.3 索引代际改造，必须同时修改 SQL 和纯契约

索引内容有效性改为：

```text
Vault 未删除
AND 来源文档/修订/fragment 未删除
AND document.current_revision_id == projection.source_revision_id
AND projection.fragment_hash == 当前 fragment.text_hash
AND 当前 SEARCH 及索引策略允许
AND tokenizer/embedding 版本与所用检索配置兼容
```

方案：新增 `search_chunk_projection`，复用现有 SearchProjection 策略校验并提取公共函数。现有 projection 表和查询暂不破坏，供旧调用保留；新链路仅使用新表，通过 feature flag 切换。

新表字段：`id, vault_id, chunk_id, source_revision_id, fragment_hash, lexical_terms_json, embedding_json, tokenizer_version, embedding_space_id, built_source_generation, built_policy_epoch, consent_record_id, index_policy, deleted_at`。一个 chunk/版本/embedding_space 的唯一约束；无向量时使用确定的 lexical space 标识。

`built_*` 只用于审计，不能当成全库内容最新性的等值条件。Repository 每次重新校验来源和当前授权，生成带当前请求 snapshot 的 RetrievalRecord。旧 build generation 不能直接改写成新值以绕过检查；只有通过回源校验后才能构建本次 record，保留 build 信息供诊断。

现有 `RetrievalRecord.source_generation` 在新适配器中明确表示“本次验证的 generation”；新增 `projection_build_generation` 表示构建时版本。所有入口都必须经过适配器，不允许调用者自行赋值获得授权。旧纯函数行为与测试保持兼容，增加新适配器测试。

任务和 ContextPack 仍保留当前全局栅栏，第一阶段允许任何源变化令进行中结果失效，宁可提示重试。后续优化到精确依赖时，必须同时审查 Gateway、ModelRuntime、job fences 和数据库函数，不能只放宽索引层比较。

### 6.4 词法与语义实现选择

首版可靠基线：沿用 CJK unigram/bigram tokenizer、词法与结构化排名、RRF。批量取候选而非逐篇调用 `list_search_projections`，防止 N+1。

先实现授权范围内的有界精确扫描；上线目标语料明确限制为 5,000 个 chunk，超出时按时间/事项筛选并显示覆盖不足，不声称检索了全部历史。达到目标规模仍慢时，再增加倒排 token 表或 PostgreSQL 索引，不通过任意截断冒充完整结果。

语义检索为下一阶段增强：

- 单独 `EmbeddingProvider` 端口，不能假设聊天 API 支持 embeddings。
- 增加单独配置或可选本地模型；未配置时词法回退，并通过 capabilities 声明。
- embedding_space_id 包含供应商标识、端点指纹、模型/版本、维度、归一化方式；不同空间绝不混排余弦距离。
- 查询与文档必须同空间；模型切换采用新空间并行重建，旧空间在新空间可用前仍可选择使用或回退词法，不混用。
- 初始 JSON 向量＋精确计算用于小规模对照；向量占用随维度和 JSON 编码膨胀，应测量后再决定存储。
- PostgreSQL vector 扩展和 Windows 二进制兼容、许可、备份恢复及迁移需要单独验证。没有验证就不在安装包中承诺 HNSW/pgvector 可用。

## 7. 记忆提取与更新

新增任务 `memory.extract.v1`，与当前 `candidate_insight` 并行保留。输出允许 `no_candidate`，不能每篇都强制产生偏好/价值/目标。

建议输出含有限的 episode candidates 和 claim candidates，每条包含原文锚点、attribution、时间表达、不确定性。模型不决定生命周期 ACTIVE、不生成 Vault/策略/任务 ID，也不自动执行工具。

落库顺序：schema 校验 → offset/hash 校验 → 来源归属及条件/否定检查 → 安全策略 → 重复/抑制检查 → KnowledgeService 候选写入。必须复用当前裁定/版本服务，不能直接 INSERT 一个 active claim。

当前 ExactSpanEvidenceVerifier 能验证位置和部分措辞保留，但不能证明语义推论成立。所有推断仍标为待确认；所谓“独立语义检查”若由模型执行，也只能辅助，不作为客观真实性证明。

更新规则：

| 新材料 | 处理方式 |
| --- | --- |
| 同一来源同一任务重放 | 幂等返回，不创建重复认识 |
| 多篇支持同一认识 | 添加可审查证据建议，不靠数量自动升级确定性 |
| 旧结论被明确纠正 | `INTERPRETATION_ERROR` 路径，旧解释不作为当前认识，保留修订记录 |
| 人生阶段发生变化 | `LIFE_STAGE_CHANGE` 路径，保留旧有效区间，当前查询选新版本 |
| 两篇说法矛盾 | 保存争议/候选关系，询问背景，不能只取最新一句覆盖 |
| 用户拒绝 | 沿用 suppression lineage；对语义近似重述增加候选匹配，但不可宣称现有 fingerprint 能解决全部近义重复 |
| 仅有短期感受 | 保留 Source/episode，不自动生成稳定身份标签 |

时间字段必须区分记录时间、事件发生时间、认识适用时间和系统写入时间。现有 temporal resolver 只覆盖少量表达；对“前阵子”“去年夏天”等未知情况保留原文与 UNKNOWN，不能猜具体日期。

## 8. 检索和 ContextPack 装配

### 8.1 查询计划

首版使用受约束的确定性计划：当前用户消息、会话近期内容、用户指定日期/日记、关联事项。必要时增加模型 query rewrite，但它只能建议关键词和时间范围，不能扩大授权、生成 SQL 或选择任意工具。

顺序：策略过滤 → 当前来源版本过滤 → 词法/语义/事项候选 → RRF → 按来源多样性去重 → 回源解密 → 填充原文和 claim → token/字符预算 → ContextPack。

普通问答默认排除未确认 claim。用户主动讨论某条候选时允许显式 inclusion，并标明候选状态。用户原话即使未转换为 claim，仍可作为经历材料，但不能将它的语义提升为永久事实。

### 8.2 反例检索的语义修正

现有 `build_context_pack` 将反例通道记录构造成 CONTRADICTS evidence。对生产检索而言，“找到了可能不同的经历”不代表逻辑矛盾。

新增 `counterexample_candidates` 字段：独立检索出的不同经历先放这里；只有已有明确 CONTRADICTS 证据关系或经过限定的关系判断后才进入 `counterevidence`。保留“模式分析必须执行独立反例检索”的检查，但区分“执行过且没有找到”和“根本未执行”。

这是契约扩展，需要改 `ai/contracts.py`、`ai/context.py` 与测试。不能只让 prompt 说“找反例”就把所有召回结果标为反证。

### 8.3 上下文预算

初始服务端预算建议：系统/任务约 15%，当前与近期对话 25%，原文材料 40%，确认认识/纠正 15%，余量 5%；按模型可用输入容量调整，另预留输出空间。数字是初始工程参数。

不能静默截断唯一反例、当前问题或关键纠正。超限时减少候选材料、提示范围不完整，必要时请用户缩小问题。tokenizer 未知的兼容模型采用保守字符估算并设置可配置输入上限，不假设统一 128K。

每次返回 coverage：授权内候选数、实际材料数、是否截断、检索模式、无结果原因。不能显示“检索了全部日记”而实际只扫描最近几十条。

### 8.4 防止助手记忆自我污染

历史 assistant 内容只用于理解对话承接，来源标记为生成内容。任何关于用户生活的事实引用仍必须回到 Source；助手说过的话不能成为下一轮认知提取的证据。会话摘要即使后续实现，也必须保留展开的 source/claim 依赖，否则不进入长期检索。

## 9. AI 对话接入现有模型运行时

### 9.1 任务注册与 Provider 解耦

新增 `conversation.reply.v1`，以 `ConversationContextAuthority` 验证 manifest 和会话 revision，以 `ConversationResultPersister` 落库答案、引用及运行产物。

将 StepFun `_messages` 的任务提示分支迁至 `ai/prompts/` 或 application 任务注册器。`ModelTaskDefinition` 扩展可信 prompt key/version；Provider 只接收服务端注册的提示和 untrusted data，不接收客户端指定 system prompt。

对话生成继续输出结构化 JSON，但 answer 字段可容纳自然语言。首版响应契约建议：

```json
{
  "answer": "根据现有记录，还不能判断你对整份工作的感受都变了。",
  "citations": [
    {"label": "S1", "quote_start": 0, "quote_end": 12}
  ],
  "response_kind": "grounded_answer",
  "follow_up_question": null,
  "memory_candidate_suggestions": []
}
```

上述 offset 是结构示意，不对应真实原文。模型仅引用本次分配的短标签 S1/S2；服务端把标签映射成真实 fragment 和绝对字符偏移。`response_kind` 可为 grounded_answer / clarification / insufficient_evidence / direct_response；没有引用时不强行制造来源。

`memory_candidate_suggestions` 是建议，不直接生成 ACTIVE。首版可先不让 reply 同时生成候选，改为用户点击“保存为理解”后单独发起任务，减少一次回复承担的职责。

普通回答和询问不必都强行附“你可能”句式。关于个人历史的陈述必须有材料支持；引用准确与解释合理分别评估。

### 9.2 扩展输入和产物契约

当前 `ModelInputKind` 仅三种身份，`ModelRunArtifactKind` 仅 knowledge/action/narrative，数据库 artifact 形状有 CHECK 和复合 FK。新增会话必须同时修改：

- `ai/contracts.py`：新增受控的 CONVERSATION_CONTEXT 身份，用 manifest ID；模型看不到它的授权决定权。
- `model_runs/contracts.py/models.py/repository.py`：新增 CONVERSATION artifact 与 `conversation_message_id`，更新唯一约束及形状 CHECK。
- extraction 若一次生成多条结果，新增 `memory_ingestion_result` 批次产物，收据指向批次，批次项再关联 episode/claim；不能让一次 run 绑定多个互相冲突的单产物。
- `RoutingModelResultPersister` 注册新类型。
- fingerprint 包括 prompt/schema/pipeline、模型配置版本、scope revision、manifest 摘要和输入身份。

不要将 assistant message 伪装成 derived claim 来绕过现有外键。新枚举要对应迁移与 contract 测试。

### 9.3 当前用户消息与零历史情况

saved 会话创建 user Source 后，当前问题始终可作为最小受授权来源。因此新用户没有历史也能对话，不必解除 Gateway 非空来源约束。

但当前消息原文不等于一条可自动长期记住的事实：默认索引排除，模型中的引用若来自当前消息需说明“你刚才提到”。只有用户明确允许的消息进入跨会话记忆。

临时会话第二阶段另做 ephemeral authority：内存保存、退出清除、无可恢复正文，公开不保证进程崩溃恢复；不可通过创建永不清理的 Source 假装临时。本轮第一阶段不暴露该入口。

### 9.4 保留三段短事务

```text
T0 提交消息事务
  检查 session revision / 单活跃轮次 / 幂等键
  保存 user Source + message + turn + job
  COMMIT，立即返回 202

准备阶段
  授权范围内读取候选，构建 manifest 和 ContextPack
  T1: ModelRuntime prepare + 收据
  T2: 重新检查权限、会话和来源，claim dispatch
  COMMIT

数据库事务外
  调用在线 Provider，按预算等待
  schema / 引用 / 输出策略校验

T3 结果事务
  检查 run、lease、取消、activation、manifest 和来源依赖
  插入 assistant message + citations + artifact
  turn succeeded + job 完成协调
  COMMIT 后结果可见
```

Provider I/O 期间不得持有事务或连接锁。沿用现有 UNKNOWN 结果语义：超时不能证明供应商没有执行，禁止自动重复收费调用。回复落库和 artifact 必须原子；job 终态若独立协调失败，应通过 run artifact 恢复，而不是再次请求模型。

### 9.5 首版异步轮询，后续才考虑流式

现有 provider 为同步 JSON，runtime 在线程执行；桌面 preload 也是 invoke/response 桥。首版 POST 返回 turn ID，GET 轮询真实阶段，完成后显示完整回答。不得用假进度/假 token 流冒充服务端流式。

建议轮询 1 秒起步，后台降频至 3–5 秒；应用重启按 turn ID 恢复。离开页面不等于取消，用户显式取消才发取消请求。

若后续增加 SSE，至少需要 AsyncProvider/stream contract、主进程订阅转发、消息序号、断线重连及清理订阅。未验证 token 只能是可撤销草稿，取消/撤权后停止显示；最终答案仍以 T3 durable commit 为准。鉴于删除/撤权后的不可收回展示风险，首轮不做该复杂度。

## 10. API 详细契约

新资源统一使用 `/v1/vaults/{vault_id}` 前缀。实际集成保持现有路由命名风格；以下为拟定契约，非当前可调用接口。

### 10.1 资源列表

| Method / 路径 | 功能 | 成功状态 |
| --- | --- | --- |
| POST `/conversations` | 创建 saved 会话及默认材料范围 | 201 |
| GET `/conversations?cursor=&limit=` | 会话分页，不返回正文 | 200 |
| GET `/conversations/{id}` | 会话、revision、当前活跃 turn | 200 |
| GET `/conversations/{id}/messages?cursor=&limit=` | 消息与可用引用分页 | 200 |
| PATCH `/conversations/{id}/scope` | 更改使用范围；不等同授予缺失 purpose | 200 |
| POST `/conversations/{id}/turns` | 保存用户消息并排队生成 | 202 |
| GET `/conversation-turns/{id}` | 阶段、错误码、完成答案引用 | 200 |
| POST `/conversation-turns/{id}/cancel` | 幂等取消 | 200 |
| GET `/conversation-turns/{id}/context` | 经当前权限过滤后的本次材料 | 200 |
| DELETE `/conversations/{id}` | 隐藏并排队清理内容和依赖 | 202 |
| GET `/memory-index/status` | 索引覆盖、积压、版本、可用模式 | 200 |
| POST `/memory-index/rebuild` | 用户选定范围内本地重建 | 202 |
| POST `/memory-ingestions` | 显式授权的提取/历史补建 | 202 |
| GET `/memory-ingestions/{id}` | 提取状态与结果批次 | 200 |
| GET `/memory-context/preview` | 范围和计数预览，默认不触发模型 | 200 |

复用现有 memories verdict API，不另造第二个确认/纠正接口。事项与事件第二阶段新增 `/matters`、`/episodes`，沿用相同分页和 revision 模式。

### 10.2 发送一轮

```http
POST /v1/vaults/{vault_id}/conversations/{session_id}/turns
Authorization: Bearer <由桌面主进程注入>
Idempotency-Key: <UUID>
If-Match: "7"
Content-Type: application/json
```

```json
{
  "message": "最近工作上让我不舒服的事情有什么共同点？",
  "material_scope": {
    "mode": "history",
    "selected_entry_ids": [],
    "time_range": null
  },
  "allow_memory_extraction": false
}
```

服务器校验 mode；selected 模式必须有明确 entry IDs，history 仍受目的授权限制。客户端不能传 provider secret、授权 fingerprint、已确认状态或任意 system prompt。

响应示例：

```json
{
  "turn_id": "11111111-1111-4111-8111-111111111111",
  "user_message_id": "22222222-2222-4222-8222-222222222222",
  "status": "queued",
  "session_revision": 8,
  "poll_after_ms": 1000
}
```

轮次完成结果含 `answer_message_id, citations, coverage, model_display_name`。引用 DTO 通过服务端回源提供 excerpt 和日期，不把完整 manifest 中所有材料复制到每次轮询。

### 10.3 幂等与并发语义

- `Idempotency-Key` 作用域为 Vault + 操作 + 会话；相同 key 相同请求返回同一轮，不再写 Source 或调用模型。
- key 相同但请求内容不同返回 409；请求 hash 使用现有 Vault HMAC，不记录原始内容。
- `If-Match` 不匹配返回 409；同一会话已有活动轮次返回 `TURN_IN_PROGRESS`，客户端可取消后重新发起。
- 首版不排队多条未完成用户输入，避免第二轮读到尚未存在的上一轮回答。
- 创建会话、改范围、删除和提取重建同样具备幂等和乐观并发约束。
- `Cache-Control: private, no-store`；分页复用不透明、Vault 绑定游标。

### 10.4 错误处理

| 错误码 | HTTP/轮次状态 | 用户行为 |
| --- | --- | --- |
| AUTHENTICATION_REQUIRED | 401 | 桌面检查本地服务身份 |
| RESOURCE_UNAVAILABLE | 404 | 不泄露其他 Vault 资源是否存在 |
| REVISION_CONFLICT / TURN_IN_PROGRESS | 409 | 刷新或等待当前轮次 |
| CONSENT_REQUIRED | 403 | 展示缺失目的及材料范围 |
| MODEL_UNAVAILABLE | 503/failed | 配置模型后显式重试 |
| CONTEXT_CHANGED | invalidated | 保留用户输入，说明材料已变化 |
| PROVIDER_OUTCOME_UNKNOWN | outcome_unknown | 不自动再次请求；用户决定是否新建尝试 |
| CITATION_INVALID | failed | 不展示冒充有据的结果 |
| INSUFFICIENT_CONTEXT | succeeded 的 response_kind | 正常解释不足，不作为系统故障 |

## 11. 授权与模型切换

### 11.1 复用 purpose，但增加实际组合校验

| 操作 | 最少需要的目的 | 附加约束 |
| --- | --- | --- |
| 本地词法索引 | SEARCH | 本地处理，无在线传输；尊重来源 exclusion |
| 仅当前对话回答 | PASSIVE_QA | 当前消息和所用前文授权成立 |
| 检索历史后回答事实问题 | SEARCH + PASSIVE_QA | 回源材料分别验证；用户会话范围只是上限 |
| 跨日记模式分析 | SEARCH + PASSIVE_QA + CROSS_RECORD_ANALYSIS | 执行独立不同经历检索 |
| 持久化推断候选 | LONG_TERM_INFERENCE | 不等同用户确认个人认识 |
| 主动推送旧经历 | PROACTIVE_RESURFACING | 还需当前展示/安全策略，不因聊天开启自动授权 |
| 回忆录 | NARRATIVE | 保留现有叙述授权链 |

当前 `ModelTaskDefinition.consent_purpose` 为单值。新增 `required_purposes` 或由 context authority 返回逐材料 purpose bindings；Gateway 在 prepare/dispatch/finalize 校验全部必要授权。旧任务单值通过兼容转换保留。

现有 `seed_local.py` 仅初始化 LONG_TERM_INFERENCE / PASSIVE_QA / NARRATIVE；不能只在 seed 中无条件追加新目的。应先提供授权状态与用户操作 API，并让桌面传递确切选项。新增持久化授权 UI 与后台命令，逐步将 seed 退回启动初始化职责。

### 11.2 供应商和激活版本

继续保留每次模型激活独立 activation。turn 固定绑定激活 ID；排队后切换模型，旧请求取消/失效，不得自动转发到新供应商。新的 turn 重新准备上下文与授权。

对话数据跨模型共享是本地业务能力，不意味着模型之间自动共享账号、密钥或服务端线程。聊天模型设置与 embedding 配置分开，避免切换聊天模型导致全库自动上传重建向量。

### 11.3 用户“别记住”与“别使用”要分开

- 不提取长期记忆：消息可留在本次聊天，但不进入跨会话索引/claim 提取。
- 不使用某篇日记：本次 manifest 排除它以及依赖它的摘要、claim、助手消息。
- 忘记某条认识：执行撤回/抑制，并从后续上下文排除相关旧结论。
- 删除原始内容：执行来源与派生内容清理；不是简单取消记忆提取。

UI 可以使用简单表述，后台必须保持这些不同语义。

## 12. 任务调度与故障恢复

现有队列仓储可复用。新增白名单 handler registry，并将候选专用领取函数扩展成受控多任务 dispatcher，或保留候选 worker 并为新类型安装专用领取函数。建议前者，但迁移期允许双 worker、互不领取对方类型。

| Job type | 队列 | 重试策略 |
| --- | --- | --- |
| source.index.v1 | INGEST_TEXT | 本地幂等，可指数退避 |
| memory.extract.v1 | INGEST_TEXT | dispatch 前安全失败可重试；未知外部结果不自动重试 |
| conversation.reply.v1 | INTERACTIVE | 同上，优先于批量历史任务 |
| dependency.invalidate.v1 | PRIVACY_CRITICAL | 可重试，期间读取实时阻断 |
| matter.refresh.v1 | REFLECTION | 显式授权后合并去重调度 |

不要把私密内容加进现有 SAFE_PAYLOAD_KEYS。领取函数只返回任务身份及 lease，worker 在 Vault 上下文中重新读取私密业务数据。

单桌面初始并发建议：在线生成 1、本地索引 1；隐私任务优先，不受批量提取阻塞。lease/heartbeat 沿用现有机制，但长生成预算必须小于可续租策略覆盖时间。

启动恢复：扫描非终态 turn，通过 job/model_run 收据恢复；succeeded artifact 存在则直接修复投影；已 dispatch 且结果未知则标 outcome_unknown。禁止把所有 running 状态统一改 queued 后重发。

Outbox 采用已有模式之一即可：保存事务直接 enqueue 是首版路径；如果使用 outbox fan-out，就必须实现并启用 materializer 和至少一次消费幂等，不能只写事件而没有消费者。

## 13. 编辑、删除、撤权与真正的失效传播

### 13.1 即时正确性优先于后台清理速度

现有 Source tombstone 会清除相关词法/向量载荷，标记 fragment/document，并提升 Vault fences；Knowledge 有 `invalidate_source_evidence` 服务。当前公共 delete_entry 路径未直接编排新的依赖链，不能假设新增对象会自动被清理。

新事务必须先建立即时阻断，再发清理任务。生成前、提交前、读取历史答案时都检查来源可用性。后台未完成前，不能继续显示来源派生的旧摘要或缓存正文。

### 13.2 各类变更

| 触发 | 同事务必须做 | 后台处理 |
| --- | --- | --- |
| 日记新修订 | current_revision 更新、generation 提升、旧索引不可选、新索引任务 | 旧 episode/claim evidence 标 stale；旧答案标待复核 |
| 来源删除 | tombstone、索引载荷清除、依赖目标禁用、取消相关任务、清理请求 | 清除新答案/摘要内容或整体遮蔽，调用 Knowledge 证据失效，清缓存 |
| SEARCH 撤回 | 当前读取立即拒绝索引使用 | 清除索引；不等于删除原始日记 |
| 在线用途撤回 | 阻止新 dispatch/commit，不能收回已发往供应商的材料 | 取消相关任务，清除短期上下文 |
| 某次 scope 缩小 | scope revision 提升，相关 turn 失效 | manifest 重新建立，不复用越界摘要 |
| claim 纠正/撤回 | revision 或状态改变，旧当前上下文失效 | 重建关联事项摘要；新检索采用当前版本 |

对答案的正文无法可靠局部剔除被删材料时，先整体遮蔽并提示“相关材料已变化/删除”，允许用户主动重新生成。不要只移除引用角标，却继续显示包含被删内容的回答。

删除会话时删除/遮蔽该会话消息和用户消息 Source；由这些消息产生的跨会话理解同样失去相应依据。是否保留明确另存为日记的副本要在操作预览中列出，不能让副本关系不可见。

### 13.3 栅栏与精确依赖的演进

第一阶段继续全局 policy_epoch/source_generation 严格检查，保证安全，可能产生保守失效。第二阶段引入独立 `knowledge_revision`（裁定/记忆状态变更递增）或等效 dependency revision，确保认识被纠正不会遗漏。

第三阶段才优化无关来源新增导致的取消：保留 Vault 删除/授权的强校验，来源改为精确 revision/hash，claim 改为精确版本/lifecycle/review_revision，scope 与 activation 精确匹配。所有旧入口保持原 fence 语义；通过新 policy_version 显式选择，禁止一刀切移除全局检查。

重复读取验证不等于消除了并发：提交事务需对相关权威记录采用确定顺序的锁或同等条件写入，确保撤权/删除先提交时旧结果不能随后成功提交。生成时不长时间持锁。

### 13.4 恢复备份与新版本空白

保留当前按 app version 隔离、首次空白和用户主动备份恢复策略，不自动合并旧目录。长期积累可在用户主动恢复后继续。

恢复新数据库后：迁移 schema、禁用在线 AI、取消/冻结所有未完成外部任务、重新生成运行时授权激活，索引通过版本校验后使用或本地重建。不要恢复“正在生成”就直接重发供应商请求。备份不含 API Key，沿用现有行为。

删除不覆盖已导出备份和旧版本目录，也不能保证供应商删除已接收材料；产品应精确展示这几个边界。

## 14. 数据库迁移与兼容发布

采用增加表/字段的 expand-first 迁移，分阶段启用读取。不要重写已应用的 Alembic revision。实际 revision ID 在实施时生成，以下仅为批次名称。

| 批次 | 变更 | 发布门槛 |
| --- | --- | --- |
| M1 conversations | session/message/turn 表、注册、RLS、FK、唯一约束 | 仅存储接口可用，无在线生成 |
| M2 context | manifest/dependency/citation；run input/artifact 扩展 | 三事务及取消/重放测试 |
| M3 indexing | processing_request、chunk/projection、检索适配器 | 新增一篇不使其他新索引不可用 |
| M4 authorization | 新授权命令路径、多 purpose 校验、worker dispatcher | 老配置不自动获得新权限 |
| M5 memory | 事件、事项、批量提取结果、修订映射 | 不产生自动 ACTIVE、不污染原文 |
| M6 dependency | 新派生对象清理与恢复、可选精确依赖 fences | 并发删除/撤权/纠正覆盖 |

以上是结构拆分，发布必须按第 17 节组合完成相互依赖；不能在 M2 后独立开启缺 M4 的历史聊天。每批同步 metadata、迁移测试、`apply_postgres_security`、受限角色权限与私有函数。

旧数据处理：

- 不把已有 ClaimVersion 批量改成新的推理结果，保留 verdict 和 lineage。
- 对旧全文 fragment 只建立派生 chunk，不改 offsets、revision IDs 或旧引用。
- 本地索引 backfill 在授权内执行；AI 提取 backfill 独立显式触发。
- 旧 candidate/action/narrative 路由继续返回原 schema。
- capabilities 新字段默认 false；老前端继续使用现有接口。

迁移前制作用户可恢复的备份并做迁移演练。桌面升级数据库可在迁移前复制/备份到独立回退位置；失败不启动半迁移业务。回滚优先关闭新能力并恢复迁移前数据库到新目录，不以 DROP 新表抹掉用户新产生内容。

## 15. 安全、可观测性与性能预算

### 15.1 不绕开当前边界

所有新表包含 Vault 复合关系与 RLS；业务连接继续受限，不用超级用户规避 join/权限问题。私有 dispatcher 函数固定 search_path、白名单 job 类型、只返回技术身份，禁止返回正文。

用户日记、导入文本、历史助手回复均是非可信数据。检索到“忽略规则”“上传所有日记”等内容不能改变授权或启动工具。模型不拿数据库查询接口、文件路径执行能力或 API 密钥。

### 15.2 内容最小化日志

记录 request/turn/run ID、任务版本、stage、耗时、材料数量、token usage（供应商返回时）、安全错误码。不得记录原文、prompt、答案、密钥、带内容的标题或供应商原始异常。

API Key 与端点配置由现有桌面安全存储管理。新表 ciphertext 的备份密钥路径必须接入 private-backup；不要生成只在临时进程存在而备份无法恢复的持久内容密钥。

建议指标：索引积压、覆盖率、旧投影排除数、检索召回/截断、被纠正旧认识误用次数、取消后错误提交次数、无来源引用次数、请求未知结果数。用户内容不做默认遥测；评测材料采用合成或用户主动导出。

### 15.3 初始预算

| 项目 | 建议目标/约束 | 验证方式 |
| --- | --- | --- |
| 提交用户消息 | p95 小于 500ms，本地暖启动 | 不含模型耗时，实际指定机器测试 |
| 本地检索 | 5,000 chunk 下 p95 小于 1s | 词法基线与语义路径分开测 |
| 对话生成 | 默认总预算 60s，可按供应商配置 | 非完成承诺，超时使用 outcome_unknown |
| 交互请求 | 初始一次 reply 调用 | 可选 schema 修复最多一次，显式计入预算 |
| 自动提取 | 去重、合并旧修订、低优先级 | 不在每次按键/草稿变更触发 |
| 全库重建 | 分批可取消，进度持久化 | 重启从批次游标恢复 |

模型上下文长度、价格与 tokens 统计不可靠时不显示伪精确费用。先按请求数与输入预算约束；供应商支持 usage 时再记录实际消耗。

## 16. 测试计划与发布阻断项

### 16.1 单元与契约

- 来源切块的中文、换行、emoji、重叠与 offset 还原。
- source build generation 与 request snapshot 分离，回源验证失败必须排除。
- 反例候选不自动转为 CONTRADICTS；模式分析缺独立检索时失败。
- 过去/现在两类纠正、UNKNOWN 时间、同名不同人、否定和引用他人。
- assistant 内容不能成为 evidence；scope 排除来源时依赖摘要同时排除。
- reply schema 的无发现/无引用结果；未知来源标签拒绝。

### 16.2 PostgreSQL 集成

- 两个 Vault 跨会话 ID/fragment ID/manifest ID 攻击，RLS 与复合 FK 都拒绝。
- 使用真实受限角色跑新表 CRUD、dispatcher 和迁移，不只使用 SQLite/fake session。
- 一篇日记新增后，其他新索引仍可检索；旧修订不会作为当前材料。
- 撤销来源 SEARCH 与撤销模型用途分别生效。
- 发送幂等重放只有一条 user Source、一个 turn、一次模型 dispatch。
- reply/提取提交一半失败，artifact 与答案/候选不能部分成功。
- 对话生成期间编辑来源、删除、改变 scope、修正 claim、切换模型，各自按规定失效。
- worker 崩溃、lease 丢失、应用重启、取消和成功竞争，终态一致且无重复外部调用。

### 16.3 桌面与备份集成

- 无模型可保存日记，未授权索引/分析不在线调用。
- 离开聊天页后返回、关闭重启，已保存输入与任务状态恢复。
- 同版本历史保留，新版本默认空白；用户主动恢复后新表数据完整。
- 恢复不带模型密钥、不重发旧任务。
- 索引和对话新增后安装包仍不包含用户内容，继续执行现有 release report。
- capabilities 控制入口，不让旧后端显示无法调用的新按钮。

### 16.4 质量对照实验

固定相同模型、提示主任务、近似输入/输出预算，比较 A 当前问题、B 简单历史 RAG、C 本文时间/纠正/依赖记忆方案。报告检索与生成分别的耗时/成本，避免把多调用的收益混同为架构收益。

先建立 60 个人工可核对案例：提取、跨记录、时间、更新、拒答各 10 个，删除/注入/错误归属 10 个。开发集与保留评测集分开，不把答案标签用作检索标签。再按原始说明运行 LongMemEval/LoCoMo 子集，明确子集与适配差异，不宣称官方全量得分。

评测分别记录：关键证据召回、引用正确率、当前事实准确率、纠正生效、缺证据拒答、用户评分的新增价值。正确地记住旧事实和解释得有帮助是两项指标。

发布阻断：任何跨 Vault 泄露、已删材料被重新送出、AI 回答写成用户事实、撤销后仍提交、重复收费重试、错误引用显示为有效。涉及个人判断的语义错误由人工复核，不依赖同一个生成模型自评过关。

## 17. 可执行实施批次

按功能依赖交付，不给未经估算的固定完成日期。

| 批次 | 交付物 | 验收后才能做什么 |
| --- | --- | --- |
| P0 基线固定 | 当前测试记录、schema 清单、feature flags、研究评测集骨架 | 开始迁移，不改变旧业务行为 |
| P1 日记可检索 | SEARCH 授权、本地 chunk/index、当前修订过滤、增量任务、状态接口 | 先内部验证历史检索准确性 |
| P2 有依据的对话 | saved 会话、turn、manifest、reply task、artifact、多目的授权、异步接口、基本删除传播 | 开启用户选定日记聊天，再开授权历史范围 |
| P3 记忆能更新 | 事件/事项、授权提取、claim 纠正入上下文、反例候选、索引补建 | 验证长期记忆优于简单 RAG |
| P4 发行收敛 | 依赖清理、恢复、实际 Windows 测试、模型适配与成本、性能 | 少量用户持续试用 |
| P5 可选增强 | 独立 embedding 配置、精确依赖优化、临时会话、流式方案 | 每项单独过门，不打包承诺 |

P1/P2 不能以“接上 LLM 能聊天”作为完成。必须能指出所用来源、说明覆盖范围、吸收纠正并在来源失效后阻断内容。P3 不能绕过 P2 的删除传播再补安全。

## 18. 文件级改造清单

| 现有位置 | 具体修改 |
| --- | --- |
| `application/source_entries.py` | 写入/修订同事务创建 processing_request + 索引 job；聊天 Source 的列表分流；删除协调 |
| `modules/sources/service.py` | 抽出公共索引策略校验；保留旧 projection 接口；新 reader 强制 current_revision |
| `ai/retrieval.py`、`ai/contracts.py` | build/request generation 语义、候选反例、覆盖与来源类型；旧测试兼容 |
| `ai/context.py` | 反例候选分离、claim 来源展开、预算与新上下文映射 |
| `application/model_gateway.py` | 多 purpose 验证、ConversationContextAuthority、可信 prompt 注册、manifest 再验证 |
| `application/model_runtime.py` | conversation artifact 路由、会话/依赖提交检查、幂等与未知结果复用 |
| `ai/stepfun.py`、`ai/compatible.py` | 任务提示移出；保持 JSON 非流式和供应商错误清洗 |
| `modules/model_runs/*` | 新输入/产物身份、字段与约束、fingerprint 覆盖 |
| `jobs/repository.py`、`application/candidate_insight_jobs.py` | 白名单 handler/dispatcher、索引/对话 worker、lease 和启动恢复 |
| `modules/knowledge/service.py` | 复用版本/裁定/证据失效；补依赖通知；当前记忆读取适配 |
| `modules/consent/*`、`scripts/seed_local.py` | 新用途显式授予/撤销；减少 seed 承担交互授权职责 |
| `platform/model_registry.py`、`platform/postgres_security.py` | 表注册、RLS、复合约束、角色与私有领取函数 |
| `api/app.py`、新 routers/composition | 新资源注入、worker lifespan、capabilities |
| `apps/desktop/electron/managed-runtime.cjs` | 新 API 路径/超时契约、scope 操作、激活版本同步 |
| `apps/desktop/electron/preload.cjs` | 首版复用 invoke；流式另案，不暴露主进程凭据 |
| `apps/desktop/electron/private-backup.cjs` | 验证新表和新正文保护密钥进入备份恢复，任务恢复禁重放 |
| `alembic/versions/`、`tests/` | 增量迁移、真实 PG 集成、契约和并发回归 |

## 19. 开发开始前的默认决定与待验证项

已按当前需求确定：Windows 本地运行；在线模型由用户配置；不要求服务器；日记优先；聊天共享授权长期记忆；来源与生成内容分开；旧功能和数据可达；发行包空白。

实施默认决定：首版 saved 对话、异步轮询、词法检索基线、单会话单活跃轮次、重要认识用户确认、在线补建显式开启、旧 API 兼容。

需要用实现和实验回答而非再次让用户选择技术栈的问题：5,000 chunk 性能是否足够；语义检索相较词法的实际提升；中文日期表达覆盖；兼容供应商 JSON 稳定性；全局栅栏保守失效频率；知识纠正和源删除的并发一致性。

本文件最初交付时仅为技术实现书。当前开发代码已完成 P1 本地检索、分段索引与后台补建，并交付 P2 的显式所选日记对话基础版。实际接口、验证和与完整方案的差异见 [第四批实施记录](./33-recall-reviewed-memory-implementation.md)；已支持本地关键词选材预览、关联认识和用户纠正进入对话上下文。会话内自动扩展材料、语义检索及 P3 事件/事项记忆更新仍未完成，不能把本方案中的全部能力视为已经上线。
