# 人生主线、回忆录与外部日历交付计划

日期：2026-08-26

## 目标

在不削弱现有 Source、Consent、Evidence、ModelRun 与用户裁定边界的前提下，交付一个新的完整闭环：

`用户确认的认识 → 生成多条证据化人生主线候选 → 用户选择范围 → 生成单章回忆录草稿 → 从用户明确选择的内容创建日历候选 → 用户最终确认或撤销`

首版不生成“唯一人生结论”，不一次生成整本回忆录，也不让模型直接调用任何外部日历。

## 数据与信任边界

1. 叙事素材只来自当前仍有效、且已经被用户确认或纠正的 Memory。
2. 每个叙事主题和章节必须保存 ModelRun lineage，并通过结构化 citation ordinal 映射回服务器持有的 Memory 与 Source Evidence。
3. 人生主线是可并存、可冲突、可拒绝的候选集合，不是人格诊断或客观事实。
4. 回忆录按单章生成；事实、反思和文学性连接在输出中分区，缺失时期必须明确标为材料空白。
5. 日历内容先保存为本地候选。模型不能选择 connector、账号、OAuth scope，也不能执行写入。
6. 外部写入必须经过用户对最终标题、时间、时区和备注的再次确认；同一 payload 只能消费一次授权。
7. 日历标题和共享字段不得包含原文引文、心理推断或敏感人物信息。

## 首版交付

### 后端

- Narrative Project：定义时间范围和用户可编辑标题。
- Life-line Generation：一次生成 1–3 条主线候选，每条包含解释、支持材料、反例与材料空白。
- Memoir Chapter Generation：基于同一项目生成一个带引用的章节草稿。
- Narrative Citation：把模型返回的材料序号解析为真实 Memory/DerivedObject，禁止模型自报数据库 ID。
- Calendar Candidate：从用户指定章节创建待确认事件，保存最终 payload hash、状态和撤销信息；首版不静默调用第三方。
- ModelRun Artifact：扩展为 narrative generation 类型，保持 exactly-one artifact 约束。

### 前端

- 新增“主线”入口，展示主题候选、引用数量和不确定性。
- 在项目内生成并阅读单章回忆录。
- 从章节创建日历候选，编辑标题、日期、时间与备注后明确确认或撤销。
- 对尚未配置的第三方账号显示诚实状态，不伪装成已写入。

## 验收标准

- 无已确认/纠正 Memory 时默认拒绝生成。
- Source、Consent、Memory 版本在模型调用期间变化时丢弃结果。
- 所有引用 ordinal 必须落在权威素材范围内，越界输出整体拒绝。
- 相同幂等键重放返回同一 generation，不重复调用模型。
- 日历候选未经最终确认不能进入可执行状态；撤销后不能执行。
- API 响应、日志与 ModelRun receipt 不包含用户正文。
- PostgreSQL migration、非 owner RLS、后端测试、mypy、ruff、前端 typecheck/build 和浏览器闭环通过。

## 明确延后

- 整本回忆录的一次性生成与 Docx/PDF 导出。
- 自动决定人生“唯一主线”。
- 未经用户逐条确认的 Todo/Calendar 批量写入。
- Google/Microsoft Calendar OAuth 与真实写入；完成候选和单次授权账本后，只剩账号授权与 provider adapter。

## 本轮实现记录与已知问题

- 已完成：受治理的人生主线与单章回忆录生成、服务器端引用序号映射、ModelRun lineage、日历候选确认/撤销及确认后 ICS 导出。
- 已完成：StepFun 真实模型 canary、PostgreSQL migration、本地认证与 vault membership 下的浏览器闭环。
- 已知 P1：叙事引用当前跳转到关联的 Memory；旧 Memory 的 Evidence fence 会在同一 Vault 新增无关 Source 后保守失效，因此后续应增加“叙事生成快照引用”只读接口，直接展示该次 ModelRun 已验证的 excerpt。
- 已知 P1：人生主线主题仍是整体 generation 级候选；逐主题确认、纠正、驳回及版本历史留到下一轮，不能把当前主题当成用户已经认可的事实。
- 已知 P2：真实 Google/Microsoft Calendar 写入、OAuth 和远端撤销尚未实现；当前“确认”只授权本地 ICS 导出，不表示已写入第三方日历。
