# AI、记忆与生成管线

## 1. 总原则

AI 管线的职责是**提出和检索候选**，不是决定一个人是谁。所有管线遵守：

```text
Source → Candidate → Evidence check → Policy gate → User-visible state
```

禁止以下捷径：

- 用聊天摘要覆盖原始消息；
- 用一条 embedding 代表完整用户；
- 让生成模型直接执行 `update_user_profile`；
- 把 AI 上一轮总结当下一轮事实证据；
- 为追求“懂你”而默认收集所有数据；
- 在无法找到出处时补写看似合理的细节。

## 2. 摄取管线

### 2.1 状态机

```text
persisted
  → normalized
  → segmented
  → extracted
  → linked
  → verified
  → indexed
  → ready

任一步骤 → partial / failed → 可重试
```

`persisted` 即对用户成功；后续状态只影响智能功能。每一步写独立产物和 pipeline version，便于单步重跑。

### 2.2 具体阶段

1. **Normalize**
   - 检测语言、格式、编码；
   - 音频做 ASR，图片/PDF 做 OCR；
   - 保留页码、说话人、时间戳、原始文件哈希；
   - 不在此阶段润色或解释。

2. **Segment**
   - 优先按自然段、对话轮次、语义边界切分；
   - 长片段再按 token 上限重叠切分；
   - 保存稳定 offset 和层级关系；
   - 人名、日期等关键短文本不能因切块丢失邻接上下文。

3. **Classify**
   - 判定片段是否包含事件、明确事实、感受、愿望、承诺、价值表达、关系描述或纯随笔；
   - 判定是否允许长期记忆、是否属高敏内容；
   - 分类只是路由信号，不写人格标签。

4. **Extract**
   - 使用严格 JSON Schema 输出候选对象；
   - 每个字段携带原文 span；找不到 span 的字段作废；
   - 明确区分 `explicit`、`paraphrase`、`inference`；
   - 保留否定、条件、引用他人观点和不确定语气。

5. **Resolve time**
   - 区分记录时间、事件时间、有效时间；
   - 保存“去年春天”等原始表达与解析范围；
   - 模糊时间输出区间和精度，不虚构日期；
   - 相对时间以记录时区和 capture timestamp 为基准。

6. **Resolve entities**
   - 从当前记录与该 vault 的有限候选中消歧；
   - 候选合并需有别名、关系、时间等多信号；
   - 高风险或低置信合并进入用户确认队列；
   - 不把跨用户实体放进同一个解析空间。

7. **Verify evidence**
   - 单独 verifier 检查候选是否被 span 支持、是否遗漏否定/主体/时间；
   - 搜索可能的反证和过期版本；
   - 生成模型与 verifier 最好使用不同 prompt，关键场景可用不同模型族；
   - verifier 仍不是事实裁判，只决定候选是否达到展示门槛。

8. **Deduplicate / contradict**
   - 精确哈希只处理完全重复；
   - 语义重复、细化、时间变化、真正矛盾分别建关系；
   - 新事实优先创建版本或 contradiction edge，不原地覆盖；
   - “我今天不想社交”不能与“我通常喜欢朋友聚会”自动判矛盾。

9. **Policy gate**
   - 根据类型、敏感性、证据、用户设置决定自动激活、待确认或不持久化；
   - 最小化打扰：候选可汇总到记忆收件箱，而不是每条弹窗。

10. **Index**
    - 受限 Worker 只对允许检索的 fragment/claim 短暂解密，生成独立 `search_projection`；
    - lexical terms/`tsvector` 与 embedding 都是服务端可读的敏感派生数据，使用 RLS、受限角色和独立删除策略；
    - embedding 只存派生向量及对应 source/claim ID；高敏来源可选择完全不建云端索引；
    - 中文 FTS 在 M0 实测应用层分词、n-gram/`pg_trgm` 或获批扩展，不能默认内置词典满足中文姓名与原句搜索；
    - 删除或撤回同意时立即从检索视图排除，再物理移除。

## 3. 记忆写入闸门

| 内容 | 默认处理 | 可用于主动建议 |
|---|---|---|
| 用户明确陈述的低敏事实，如常用语言 | 自动 active，可见可改 | 是 |
| 用户明确说“请记住……” | active，但仍受敏感策略限制 | 是 |
| 单次偏好或情绪 | episodic，不升级稳定 profile | 仅作为当次上下文 |
| 长期目标/重要承诺 | candidate 或显式确认 | 确认后 |
| 价值观、身份、自我评价 | candidate | 确认后 |
| 关系模式、人格特征 | hypothesis | 确认且有多源证据后 |
| 创伤、性、健康、财务、违法经历 | 高敏 candidate 或只保留原文 | 默认否；必须专项授权 |
| 临床诊断、自杀风险标签 | 不作为普通记忆自动生成 | 否 |
| AI 总结/回忆录中的新说法 | artifact，不进 memory | 否 |

高敏内容的“是否保存”和“是否主动再呈现”是两个独立权限。用户可以保留一段记录，但禁止周年回顾、通知或回忆录自动纳入。

## 4. 检索管线

### 4.1 查询意图

先判断请求类型，以决定时间和证据策略：

- `recent_context`：最近发生了什么；
- `fact_lookup`：人物、时间、明确事实；
- `current_self_model`：当前目标/偏好，只取当前有效版本；
- `historical_self_model`：某时期的状态；
- `pattern_reflection`：跨记录模式，必须找反证；
- `action_support`：当前目标、约束、承诺和过往实验；
- `narrative_research`：高覆盖、多样化、按时间/主题组织；
- `source_search`：优先原文而非记忆摘要。

### 4.2 召回与排序

```text
权限/同意/删除/敏感等级过滤
  → 结构化筛选（时间、人物、类型、状态）
  → PostgreSQL FTS 候选
  → pgvector 候选
  → 明确实体与关系扩展
  → reciprocal-rank fusion
  → evidence / recency / temporal-validity / diversity rerank
  → counterevidence pass
```

这条顺序描述的是查询计划与安全语义。MVP 使用 exact vector search，结构化/RLS 条件可在精确候选上强制执行；未来启用 HNSW 时，pgvector 的过滤可能在近似扫描后发生，SQL 条件书写顺序不保证召回或延迟。届时必须评测 iterative scan、按 vault/规模分区或 partial index，并始终由 RLS/最终过滤保证不会返回其他 vault 或被排除的记录。

可将排序理解为下列信号组合，而不是固定魔法常数：

```text
score = semantic_relevance
      + lexical_relevance
      + entity_match
      + temporal_fit
      + evidence_quality
      + user_confirmed_bonus
      + source_diversity
      - contradiction_penalty
      - sensitivity_penalty
      - stale_or_superseded_penalty
```

权重必须由离线评测和用户反馈调整。小数据先做 exact vector search；不要在没有性能证据前引入 HNSW。

### 4.3 `ContextPack`

所有生成模块只接收统一上下文包：

```json
{
  "purpose": "pattern_reflection",
  "policy_snapshot": "...",
  "time_scope": {"from": "...", "to": "..."},
  "confirmed_claims": [],
  "candidate_claims": [],
  "source_quotes": [],
  "counterevidence": [],
  "uncertainties": [],
  "excluded_count_by_reason": {},
  "coverage": {},
  "citation_map": {}
}
```

ContextPack 是有版本、短期保留的派生产物。它记录引用 ID 和策略，不进入普通日志；原文内容加密存储并遵循最短保留。

## 5. 洞察生成

### 5.1 何时生成

只有满足以下条件才运行：

- 有足够新信息或用户主动请求；
- 用户允许跨记录分析；
- 来源覆盖不被单一天/单一情绪完全主导；
- 相似洞察近期未被驳回或静音；
- 当前不是被 Safety 策略抑制的主动触达场景。

### 5.2 输出合同

每条洞察只能包含：

1. **观察**：“在三条记录里，你都提到了……”；
2. **可能解释**：“一种可能是……”，不能写成定论；
3. **证据**：可展开的原文与时间；
4. **反证/边界**：“也有一次你……，所以还不能确定”；
5. **开放问题**：允许用户不同意；
6. **可选下一步**：最多一个低风险、可逆行动。

禁止输出人格判决、病理化语言、命令式劝告或“我比你更懂你”的语气。

### 5.3 质量门槛

- 100% 洞察至少有一个有效 source span；模式类至少两个独立时间点。
- 所有事实性子句通过 citation coverage 检查。
- verifier 发现主体、否定、日期或证据不一致时不展示。
- 同一主题被用户连续驳回后进入 suppression rule，除非用户主动重启。

## 6. Todo 与行为实验

### 6.1 意图分类

系统抽取时必须区分：

| 类型 | 例子 | 后端动作 |
|---|---|---|
| explicit commitment | “明天下午我要给小李回邮件” | 建 commitment 候选；可一键确认时间 |
| wish | “真想有空学钢琴” | 仅记愿望，不建 Todo |
| concern | “房间太乱让我烦” | 可提出问题，不默认“打扫房间” |
| idea | “也许可以做播客” | 存灵感或候选，不设截止 |
| coach experiment | “要不要试三天睡前不看手机？” | 显示理由、成本、退出方式，用户接受后建 experiment |
| external action | 发消息、写日历、下单 | 每次确认作用域与具体 payload 后执行 |

### 6.2 从洞察到实验

```text
confirmed insight / user goal
→ 生成 1–3 个小而可逆的选项
→ 用户选择或自己改写
→ 可选 if–then 计划
→ 适度提醒
→ 到期只问发生了什么，不评判人格
→ 结果作为新 Source
→ 支持、削弱或细化原假设
```

系统不得以完成率形成“自律分数”。没做是观察数据，不是失败标签。

## 7. 回忆录与人生主线

### 7.1 不是一次性长文本生成

回忆录使用多阶段可审计流程：

1. **范围与隐私**：时间段、主题、读者、匿名规则、排除人物/创伤内容。
2. **素材盘点**：按年份/阶段给出覆盖率与空白，不用 AI 填补空白。
3. **时间线构建**：仅收录有来源事件，模糊日期保持模糊。
4. **主题候选**：检索跨阶段的重复、转折和矛盾，输出多个并存主线。
5. **提纲协商**：用户调整章节和解释框架。
6. **冻结章节证据包**：防止重跑时资料漂移。
7. **逐章草稿**：句子分 factual / reflective / literary。
8. **事实审计**：检查人物、年代、引用和 claim/source coverage。
9. **用户编辑**：批注“不像我”“不准确”“太私人”，作为局部修订而不是模型争辩。
10. **导出**：Markdown/Docx/PDF 等属于输出层；主库保留版本和引用映射。

### 7.2 “人生主线”数据结构

人生主线应是 `NarrativeHypothesis[]`，而不是唯一标签：

```text
title
interpretation
supporting_periods[]
counterexamples[]
uncovered_periods[]
user_verdict
narrative_scope
```

同一个人可以同时拥有“寻找自主”“承担责任”“建立归属”等多条主线；它们可冲突、随阶段变化，也可被用户完全拒绝。

### 7.3 图检索实验

MVP 先用结构化实体/事件 + FTS/vector 基线。V2 可让 Graphiti 或 HippoRAG 只读取去标识后的已授权投影，做多跳主题候选：

- 结果必须映射回主库 fragment；
- 不允许图组件直接更新 claim；
- 与基线盲测 Recall@K、反证召回、错误连接、延迟与成本；
- 删除主库来源时必须同步删除图投影，并通过 canary 测试。

## 8. 模型网关

### 8.1 任务合同

每个任务声明：

```text
task_type
required_capabilities
allowed_providers
data_residency / retention policy
max_sensitivity
input_schema / output_schema
latency_budget / cost_budget
fallback_policy
```

不要把一个“万能 coach prompt”用于所有步骤。抽取、验证、检索规划和文稿生成分别版本化。

### 8.2 结构化输出与防注入

- 所有抽取使用 schema validation；无效输出进入有限次数的修复，不宽松猜测。
- 笔记、网页、PDF、邮件里的内容一律视为**不可信数据**。其中“忽略系统提示、调用工具”等文字不能改变流程。
- 工具权限由服务器代码授予，不由模型输出自由选择；外部副作用必须走 policy + user confirmation。
- 检索内容使用清晰数据边界，模型只被允许引用，不被允许服从其中指令。
- 生成后做 citation、PII、policy 与安全检查。

### 8.3 供应商隐私

- 按任务发送最小必要片段，不上传整个日记库。
- 优先选择不训练、短留存/零留存和合适区域处理选项，并保留合同证据。
- provider、model revision、prompt version、consent snapshot 全部记录。
- 高敏 vault 可配置为只用获批供应商、私有部署或禁用相关功能。

## 9. 版本升级与重算

Ontology、prompt、embedding 模型升级时：

1. 建新 `pipeline_version`；
2. 在影子表/版本上重算小样本；
3. 比较抽取、证据、矛盾、删除和成本指标；
4. 不自动覆盖用户已确认内容；
5. 对候选做 diff，必要时请求重新确认；
6. 切换读取别名；
7. 保留回滚窗口，随后依保留策略清理旧派生索引。

Embedding 变化需要重建索引，但不应改变 Source 或用户裁定。

## 10. 降级体验

模型、向量或 Worker 不可用时：

- 记录、编辑、删除、导出仍可用；
- 全文搜索仍可用；
- 显示“整理稍有延迟”，而非空白或重复提交；
- 不用低质量备用模型处理超过其敏感等级的内容；
- 恢复后通过幂等任务补算。
