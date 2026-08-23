# 路线图与评测计划

## 1. 实施策略

先证明三件最难且最不可替代的能力：

1. 系统能从零碎记录中抽出**有精确出处、时间正确、可由用户纠正**的生命材料；
2. 系统能提出**不像算命、不会静默固化身份**的洞察和行动候选；
3. 删除、撤回与隐私范围能贯穿所有派生层。

聊天皮肤、丰富图谱、花哨数据看板和完整回忆录排版都排在其后。

以下是按退出条件组织的里程碑，不是固定日历承诺。若以约 2 名后端、1 名客户端/前端、1 名产品设计并有兼职安全/心理顾问估算，可把 M0–M3 视作约 8–12 周的封闭 MVP；真实工期应在 M0 PoC 后重估。

## 2. M0：两周架构与检索 PoC

### 目标

验证自研 PostgreSQL 基线是否足够，并量化 Graphiti/Mem0 的真实增益与代价。

### 合成数据集

建立不含真实用户隐私的 `LifeLog-500`：

- 500 条跨 5 年记录；
- 50 个同名、代称或关系变化人物；
- 100 次偏好/目标变化；
- 50 个“过去成立、现在不成立”的事实；
- 50 个明确反证；
- 30 次源文修订或删除；
- 模糊时间、否定、转述、反讽、愿望与承诺的困难样例；
- 一套独立安全集：覆盖过去/当前、本人/第三方、真实陈述/引用/虚构、否定/隐晦表达、普通痛苦、身体急症、创伤再呈现及多地区语言。

每个样例标注 event/person/time、claim、evidence span、contradiction、expected current/history answer 和 deletion set。

### 对比实现

1. 自研 PostgreSQL + FTS + pgvector；
2. Mem0 OSS 适配器，作为扁平记忆基线；
3. Graphiti OSS 适配器，作为时态图基线。

所有适配器只能读取同一份测试投影，不能使用托管版专有优化，也不能直接写主模型。

### 退出条件

- 能重放同一数据集和固定查询集；
- 每个结果映射回精确 source span；
- 量出抽取、时态、实体合并、反证、删除残留、Recall@10、P95 与单条成本；
- 写出 Graphiti/Mem0 的 adopt / defer / reject ADR；
- 默认仍以 PostgreSQL 基线进入 M1，除非替代方案在关键指标上有明确、可重复的净增益。

Graphiti 的采用门槛不是“图看起来更高级”，而是在跨阶段/多跳查询上显著提升 nDCG 或反证召回，同时错误连接、删除完整性、延迟和运维成本均在可接受范围。

## 3. M1：可信记录核心

### 范围

- 账号、vault、RLS、设备会话；
- 文本记录时间流；
- immutable revision 与冲突保护；
- 对象上传骨架；
- consent/policy；
- outbox/jobs；
- 原文全文搜索；
- 导出与删除级联；
- 用户可见审计基础。

### 明确不含

长期人格记忆、自动洞察、主动通知、外部 Todo、回忆录生成。

### 退出条件

- 模型完全离线时仍能保存、编辑、搜索、导出、删除；
- 跨 vault 测试零泄漏；
- 删除 canary 在在线存储/索引中 100% 清除；
- 重复请求和 Worker 重跑不产生重复 revision/job；
- 日志与 trace 扫描无用户正文。

## 4. M2：可审计记忆

### 范围

- 文本切片与稳定证据锚点；
- 事件、人物、明确事实、愿望/承诺抽取；
- 时间解析与实体候选；
- evidence supports/contradicts；
- candidate/active/disputed/superseded/retracted；
- 记忆收件箱、来源解释、确认/修正/驳回；
- FTS + exact pgvector 混合检索；
- Model Gateway 与结构化输出版本。

### 退出条件

- 所有展示的 memory 都有原文 span；
- 模型输出不能绕过 proposal 接口直接写 active 高风险记忆；
- 用户修正创建版本链，历史查询仍正确；
- 删除 Source 后相关 evidence、embedding 和只依赖它的 candidate 不再出现；
- 对困难集的承诺识别优先保证 precision，愿望不会大量变 Todo。

## 5. M3：反思与行动闭环

### 范围

- on-demand 与低频周期回顾；
- ContextPack、反证搜索和 citation verifier；
- observation / interpretation / uncertainty 分层；
- 动机性访谈式开放问题；
- 用户选择的小实验；
- if–then 计划与温和提醒；
- 结果复盘并更新 hypothesis；
- Safety 输入/输出网关与创伤再呈现策略。

### 退出条件

- 人工盲评中，事实性句子均可追溯；严重无依据身份判断为 0；
- 用户可以“不认同”而不必与系统争辩，驳回会真正抑制同类主动洞察；
- 系统不把未确认 suggestion 变成任务；
- 所有外部副作用仍未启用，或逐项最终确认；
- 危机、安全、创伤和普通负面情绪情景通过专项测试。

M3 后且通过试点安全准入门槛，才邀请小规模真实用户，并先从用户主动请求的回顾开始，暂缓全自动主动推送。准入门槛包括：已确定首发地区与年龄门槛、当地资源已核验、事件响应负责人和升级路径到位、知情同意与随时退出可用、目标人群已明确。初始普通用户试点排除正在发生的迫近危机；若要招募心理困扰人群，需另立方案并配置临床安全监督。

## 6. M4：人生手稿 Alpha

### 范围

- 时间线与材料覆盖报告；
- 多条 NarrativeHypothesis；
- 范围、人物匿名和主题排除；
- 可编辑提纲；
- 冻结章节 ContextPack；
- 逐章生成、逐句引用和不确定性检查；
- Markdown 导出，其他格式后置。

### 退出条件

- 时间/人物事实错误率达到内测门槛；
- 无来源空白不会被模型编造补齐；
- 删除/排除来源能使受影响章节标记过期并可重建；
- 用户可以把一句标成 factual / reflective / literary 并改写；
- 回忆录文本永不反向写成用户事实。

## 7. M5 以后：按证据扩展

只有真实使用证明价值后再考虑：

- Graphiti/HippoRAG 图检索生产化；
- 音频、图片、PDF 全量摄取；
- Todoist/日历等连接器；
- Hatchet durable workflow；
- 客户端 SQLite、本地 embedding 与真正 local-first 同步；
- 多人协作编辑回忆录；
- 自托管/私有模型；
- 多语言叙事与专业排版。

## 8. 评测体系

### 8.1 抽取与时态

| 指标 | 含义 |
|---|---|
| Span precision/recall/F1 | 事件、人物、事实是否对应正确原文 |
| Negation accuracy | “不想/不是/没发生”是否保留 |
| Speaker attribution | 用户观点与他人转述是否区分 |
| Temporal interval accuracy | 事件时间与有效区间是否正确 |
| Time precision honesty | 模糊时间是否被错误精确化 |
| Entity merge/split error | 同名误合并与同人误拆分 |
| Commitment precision | 明确承诺中有多少是真的；优先于 recall |

### 8.2 检索

- Recall@5/10、MRR、nDCG；
- exact name/date/quote 查询成功率；
- current 与 historical 版本正确率；
- contradiction/counterevidence recall；
- source diversity 和单日材料支配率；
- deleted/suppressed/sensitive leakage = 0；
- P50/P95 latency 与每查询 token/cost。

### 8.3 洞察

由至少两位盲评者按量表判断：

- groundedness：每个事实性子句是否有证据；
- calibration：是否把可能性写成定论；
- counterevidence：是否呈现重要例外；
- specificity：是否基于这个用户，而非通用鸡汤；
- autonomy support：是否保留选择权；
- non-pathologizing：是否避免诊断和人格判决；
- actionability：下一步是否小、可逆、符合用户目标；
- resurfacing appropriateness：是否不恰当地再呈现敏感材料。

上线采用零容忍门槛：在注明样本量、覆盖范围和不确定性的测试窗口内，严重无来源事实、错误诊断式表述、越权外部行动的观察数均为 0；任一事件阻断发布并触发复盘。此门槛不构成真实世界零风险保证，也不能被平均分掩盖。

### 8.4 回忆录

- factual sentence citation coverage；
- 人物、日期、地点一致性；
- source coverage 与时间空白可见性；
- 同一素材跨章节自相矛盾率；
- 删除/排除来源后的残留；
- 用户“准确、像我、愿意保留”的逐章评分；
- 不把文学连接误标成事实。

### 8.5 安全与隐私

- 跨 vault API/FTS/vector/export 渗透集；
- prompt injection 与恶意附件；
- 模型供应商 policy fallback；
- 日志、trace、dead-letter 敏感内容扫描；
- 删除 canary 全 sink；
- 普通情绪、强烈痛苦、明确危机、第三方风险、未成年人等情景；
- 创伤周年回顾、通知锁屏预览、共享日历标题等再呈现风险。

## 9. 线上实验原则

- 不拿高风险安全响应做无保护 A/B 测试。
- 不优化“用户确认洞察率”，否则模型会变得讨好、笃定。
- 不以更长对话、更多通知、更高记录量替代用户福祉。
- 新 reflection 模型先 shadow run，用历史合成/明确授权数据盲评，不直接触达。
- 每次模型/prompt/ontology 变更保留版本、样本差异和回滚。

## 10. 推荐北极星与护栏

### 北极星

```text
每月完成的“确认洞察 → 自选实验 → 结果复盘”闭环人数比例
```

它同时要求洞察有用、行动是用户选择、系统会回来听结果。

### 护栏

- 无依据/不准确反馈率；
- “太私人/不想再看到”反馈率；
- 主动通知关闭与卸载；
- 高风险记忆未经确认使用次数；
- 删除完成时间与残留；
- 数据泄露/越权为零；
- 用户报告的压力、羞耻或依赖信号。

## 11. 尚需产品负责人做出的决策

这些问题不阻塞 M0，但会影响 M1 以后：

1. 第一目标用户是普通自我反思、职业成长，还是心理困扰人群？后者会显著提高临床治理要求；必须在真实用户试点准入评审前定案。
2. 首发是 Web、原生移动端还是桌面？离线需求会改变 Source Vault 边界。
3. 服务地区与数据驻留要求是什么？决定供应商与基础设施区域。
4. 商业模式是否允许云端处理私密数据，还是“本地优先”本身就是核心卖点？
5. 主动提醒默认关闭还是 onboarding 中显式开启？建议默认关闭。
6. 是否允许用户分享回忆录给他人？一旦允许，需要处理文中第三方隐私。
7. 是否会宣称“心理健康/治疗”效果？建议 MVP 明确不做，避免产品和证据责任失控。

## 12. 第一个开发迭代的可执行 Backlog

1. 建立领域术语表、JSON Schema 与状态机测试。
2. 建 PostgreSQL migration：vault、source、revision、fragment、outbox、job、consent、audit。
3. 实现 `POST/GET/PATCH/DELETE entries` 与 idempotency。
4. 实现 RLS、双 vault canary 和内容禁止日志中间件。
5. 实现 Worker lease、重试、dead-letter 和 SSE 状态。
6. 建 `LifeLog-500` 生成器、人工 gold schema 与评测 runner。
7. 实现 FTS/pgvector 基线和 ContextPack。
8. 实现结构化抽取 + evidence span verifier。
9. 实现 memory inbox 与 verdict 版本链。
10. 接入 Mem0/Graphiti 的只读 PoC 适配器并写 ADR。
