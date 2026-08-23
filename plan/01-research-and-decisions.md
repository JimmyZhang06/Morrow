# 研究依据与架构决策

## 1. 调研方法与限制

本轮优先核对项目官方仓库、官方架构文档、许可证和原始论文，而不是依赖二手产品文章。调研结论截至 2026-08-23；开源项目的功能、许可证与维护状态仍需在正式引入依赖时再次核验。

这里的目标不是寻找一个“一键变成 life coach”的框架，而是分别回答：

- 怎样降低记录阻力；
- 怎样保留原始材料与来源；
- 怎样表达随时间变化的人生事实；
- 怎样从长历史中可靠召回；
- 怎样让后台反思可审计、可重跑；
- 怎样避免模型把猜测写成用户身份。

## 2. 开源项目结论

### 2.1 记忆与知识层

| 项目 | 当前定位 | 许可证 | 借鉴 | 不直接采用的原因 |
|---|---|---:|---|---|
| [Graphiti](https://github.com/getzep/graphiti) | 增量式时态 context graph | Apache-2.0 | Episode 来源、事实有效期、系统记录期、混合检索 | 图三元组不足以表达确认、反证、目标状态；多一套图数据库；LLM 实体合并有风险 |
| [Mem0](https://github.com/mem0ai/mem0) | 通用记忆服务 | Apache-2.0 | OSS：记忆 CRUD、history 与可配置模型/向量后端；托管 Platform v3 另有异步与多信号/时态增强 | 扁平事实不是生命模型；Platform 能力不能归因给 OSS；自动抽取会污染身份模型 |
| [Letta Code](https://github.com/letta-ai/letta-code) | 状态化 agent harness | Apache-2.0 | 核心/历史/外部记忆分层、后台 reflection、版本审计 | Agent 自我改写的目标与“用户共同校订的人生模型”冲突 |
| [LangMem](https://github.com/langchain-ai/langmem) | 长期记忆编排 SDK | MIT | semantic / episodic / procedural；profile / collection；热路径与后台路径 | 不提供完整多租户、证据、删除和安全服务；非 LangGraph 项目没必要为概念绑定生态 |
| [Cognee](https://github.com/topoteretes/cognee) | 图、向量、关系库的数据管线 | Apache-2.0 | 可组合 ECL、ontology、检索策略适配器 | MVP 依赖和运维面过大；自动 improve 不符合确认原则 |
| [HippoRAG 2](https://github.com/OSU-NLP-Group/HippoRAG) | 图传播式关联检索研究实现 | MIT | 跨年份、多跳“人生主线”候选召回 | 不是生产记忆服务；错误图边可能被 PageRank 放大；无确认与时态治理 |
| [Supermemory](https://github.com/supermemoryai/supermemory) | 通用 AI memory 平台 | MIT | 多模态摄取、矛盾与遗忘的产品表达 | 自动画像/遗忘必须另加用户控制；公开基准不能代替本项目评测 |

关键核对：原 `letta-ai/letta` 仓库当前主要是落地页，旧 V1 服务端位于不再支持的 archive 分支；当前实现应看 [letta-ai/letta-code](https://github.com/letta-ai/letta-code)，不能依据旧教程选型。Core/recall/archival 等分层主要是 MemGPT/旧 Letta 的概念来源；当前仓库可直接核验的是 MemFS/Git 版本、可见记忆和后台 reflection 等能力，二者在选型表中不能当成同一版本实现。

### 2.2 记录、同步与检索基础设施

| 项目 | 借鉴 | 主要警告 |
|---|---|---|
| [Memos](https://github.com/usememos/memos) | 时间流、Markdown、低摩擦记录、单体部署 | server-first 不等于 local-first，更不等于 E2EE |
| [Joplin](https://github.com/laurent22/joplin) | 本地 SQLite、同步抽象、同步层 E2EE、逐条同步状态 | 根仓 AGPL；Joplin Server 另有商业限制，学设计但不复用服务端 |
| [Logseq](https://github.com/logseq/logseq) | Daily journal、块级引用、反向链接 | 不把 outliner/查询语言暴露给普通用户；DB/RTC 状态需谨慎；AGPL |
| [AppFlowy](https://github.com/AppFlowy-IO/AppFlowy) | 本地数据库、检索与 CRDT 分层 | 编辑器、协同与部署复杂度远超 MVP；客户端 AGPL，云端 open-core |
| [pgvector](https://github.com/pgvector/pgvector) | 在 PostgreSQL 内做结构化过滤 + 向量 + FTS | 它只是派生索引，不表达事实版本、证据、确认或权限 |
| [Hatchet](https://github.com/hatchet-dev/hatchet) | AI 后台任务的重试、限流、等待、DAG | 中期再引入；工作流参数不能放私密正文 |
| [Temporal](https://github.com/temporalio/temporal) | 幂等、事件历史、长等待、可重放思维 | 首版部署和确定性约束过重 |

### 2.3 允许复用与仅作参考

- Apache-2.0：保留许可证与版权声明；仅当上游提供 NOTICE 时传递 NOTICE，并遵守修改说明与专利条款。
- MIT：在再分发中保留版权与许可文本。
- pgvector 的 PostgreSQL License：按其许可证文本保留版权与许可声明。
- AGPL 项目可用于产品和架构研究；闭源网络服务若修改或结合受覆盖代码，必须先由专业人士做许可证评估。
- Joplin 根仓的 AGPL 与 Joplin Server 的个人使用许可证分别评估；AppFlowy 公共 AGPL 仓库与其闭源商业/托管实现也不能混为同一授权范围。
- “托管产品的性能或功能”不能自动归因到同名 OSS 包，PoC 必须跑本地开源版本。

## 3. 最值得直接采用的思想

### 3.1 从 Graphiti 借“双时态 + 来源”，不借主存储

每个事实至少同时回答：

- `valid_from / valid_to`：它在用户生活中何时成立；
- `recorded_at / superseded_at`：系统何时知道、何时被替代；
- `evidence_span_ids`：依据了哪些原文；
- `verdict`：用户确认、修正、驳回还是尚未处理。

例如“我住在上海”不是永恒 profile 字段，而是一个带现实有效期的事实版本。旧版本失效但不消失，历史问题仍能得到正确答案。

### 3.2 从 Letta 借“可见记忆”，不借自主身份改写

系统应提供类似版本控制的能力：为什么记住、何时改变、此前是什么、怎样撤回。后台 reflection 可以提出合并或过期建议，但不得静默重写用户的价值观、人格、重要关系或人生主线。

### 3.3 从 LangMem 借分类，但扩展为本项目语义

- `episodic`：发生过的事件与体验；
- `semantic`：用户明确陈述或确认的事实、偏好、价值；
- `procedural`：系统如何更适合地陪伴这个用户，例如“不要连续追问”；
- `hypothesis`：模型从多条证据中提出、尚待验证的模式；
- `commitment`：用户明确承诺的行动，不与愿望或想法混淆。

其中 procedural 记忆也必须可见、可关闭，避免教练风格无声漂移。

### 3.4 从 Joplin 借“原始内容先可靠保存”

记录成功不依赖模型成功。AI 抽取、embedding、主题聚类和文稿都可失败、重试或重建；原文写入必须先完成并立即可读。

## 4. 架构决策记录（ADR 摘要）

### ADR-001：自建可审计的生命模型作为唯一系统记录主库

**决定**：PostgreSQL 保存原始材料、版本化派生物、证据关系和用户裁定。这里的 source of record 表示“系统可靠记录了用户说过/上传过什么”，不证明记录中的事件客观发生。第三方记忆/图组件只能读取已授权投影，输出作为候选或索引，不能直接反写主库。

**原因**：产品差异和最大风险都在“系统如何理解一个人”，这层不能外包给黑盒自动记忆。

### ADR-002：采用模块化单体，而非微服务

**决定**：一个 API 部署单元、一个 Worker 部署单元、一个 PostgreSQL 集群；代码按领域模块隔离。

**原因**：早期需要跨模块事务、快速修改 ontology 与有限运维。服务边界只有在独立扩缩、团队所有权或隔离需求出现后才拆。

### ADR-003：关系数据库是真相，向量和图是派生索引

**决定**：首版用 PostgreSQL FTS + pgvector 精确检索；数据量和评测需要时再建 HNSW。Graphiti、HippoRAG 或其他图检索只做可删除、可重建的投影。

**原因**：向量相似不是事实；图边也可能是模型误判。两者都不能承担权限、版本、删除和证据责任。

### ADR-004：模型输出永不自动成为证据

**决定**：证据只能指向用户原始记录、用户修订内容或用户明确采纳的表述。AI 生成的摘要、洞察和回忆录只可引用证据，不能互相循环引用后变成“事实”。

**原因**：阻止 summary-of-summary 漂移与虚构自我强化。

### ADR-005：高风险长期记忆必须确认

**决定**：人格、心理状态、价值观、重要关系判断、创伤、人生主题、长期目标等只能进入 `candidate`；用户确认后才可用于主动建议。明确低风险事实可自动激活，但仍可见、可修改。

**原因**：一句随手记录不应被固化为身份。

### ADR-006：异步任务先用数据库队列

**决定**：用 transactional outbox + `jobs` 表、`FOR UPDATE SKIP LOCKED`、幂等键、重试和死信。复杂 DAG、跨日等待、并发治理出现后评估 Hatchet；Temporal 仅在严格重放成为真实需求时考虑。

### ADR-007：默认服务器架构，但不冒充 E2EE

**决定**：MVP 提供传输加密、静态加密、每用户密钥、最小化模型传输和可删除语义索引。若服务器需要做语义检索，它就必须能处理 embedding 或明文片段，因此不能声称真正端到端加密。

**后续方向**：客户端 SQLite + 本地 embedding/检索 + 只同步密文；这是独立产品与架构阶段，不在 MVP 中用模糊文案提前承诺。

### ADR-008：心理学只约束方法，不赋予诊断权

**决定**：使用叙事身份、动机性访谈、自我决定、ACT 价值澄清、实施意图等原则组织反思与行动；不依据聊天做临床诊断，也不以量表或模型标签冒充结论。

## 5. 原始资料索引

### 记忆与 Agent

- Graphiti：[README](https://github.com/getzep/graphiti/blob/main/README.md)、[时态边字段](https://github.com/getzep/graphiti/blob/main/graphiti_core/edges.py)、[论文](https://arxiv.org/abs/2501.13956)
- Mem0：[仓库](https://github.com/mem0ai/mem0)、[架构资料](https://github.com/mem0ai/mem0/blob/main/skills/mem0/references/architecture.md)、[评估说明](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-evaluation.mdx)、[论文](https://arxiv.org/abs/2504.19413)
- Letta Code：[README](https://github.com/letta-ai/letta-code/blob/main/README.md)、[reflection](https://github.com/letta-ai/letta-code/blob/main/src/agent/subagents/builtin/reflection.md)、[MemGPT 论文](https://arxiv.org/abs/2310.08560)
- LangMem：[README](https://github.com/langchain-ai/langmem)、[概念指南](https://github.com/langchain-ai/langmem/blob/main/docs/docs/concepts/conceptual_guide.md)
- Cognee：[README](https://github.com/topoteretes/cognee)、[cognify 实现](https://github.com/topoteretes/cognee/blob/main/cognee/api/v1/cognify/cognify.py)、[论文](https://arxiv.org/abs/2505.24478)
- HippoRAG：[仓库](https://github.com/OSU-NLP-Group/HippoRAG)、[HippoRAG 2 论文](https://arxiv.org/abs/2502.14802)

### 记录、同步与任务

- Memos：[README](https://github.com/usememos/memos/blob/main/README.md)
- Joplin：[架构](https://github.com/laurent22/joplin/blob/dev/readme/dev/spec/architecture.md)、[同步](https://github.com/laurent22/joplin/blob/dev/readme/dev/spec/sync.md)、[E2EE](https://github.com/laurent22/joplin/blob/dev/readme/apps/sync/e2ee.md)
- Logseq：[DB 版本说明](https://github.com/logseq/docs/blob/master/db-version.md)
- AppFlowy：[客户端](https://github.com/AppFlowy-IO/AppFlowy)、[云端](https://github.com/AppFlowy-IO/AppFlowy-Cloud)
- pgvector：[README](https://github.com/pgvector/pgvector/blob/master/README.md)
- Hatchet：[README](https://github.com/hatchet-dev/hatchet/blob/main/README.md)
- Temporal：[架构](https://github.com/temporalio/temporal/blob/main/docs/architecture/README.md)、[Workflow 生命周期](https://github.com/temporalio/temporal/blob/main/docs/architecture/workflow-lifecycle.md)
