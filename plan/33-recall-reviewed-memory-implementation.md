# 本地选材与用户纠正接入：第四批实施记录

日期：2026-09-11。接续 [对话基础版](32-evidence-conversations-implementation.md) 与 [整体技术方案](29-long-term-memory-chat-technical-spec.md)。

## 本批完成的体验

现在可以从问题开始：在“对话”页写下想讨论的事情，点击“查找相关日记”。系统复用本地关键词索引，展示命中原话、检查数量和待补建数量，预选最多 8 条日记。用户可调整选择，确认发送说明后建立会话；先前输入的问题会带入输入框，由用户发送。

搜索在本机执行，不调用在线模型，也不会自动开启 SEARCH 授权。未开启时引导到记录页管理搜索与补建。无命中明确提示不等于没有相关经历。会话材料仍然固定，不会在后续追问中悄悄扩大。

建立会话时可勾选“参考关联的已确认、已纠正认识及其原话依据”。开启后，每轮重新读取所选日记关联的当前认识版本。完成的回答可展开查看本轮参考的认识、确认/纠正状态和版本号。

用户在认识页纠正后，旧回答会标为过时并隐藏；下一轮不会将这些旧回答作为历史上下文发送。纠正后的新表述和用户纠正原话进入新上下文。未裁定、拒绝、暂缓和撤回的认识正文不进入提示词，纠正过的版本后来被拒绝也不例外。

## 为什么单独增加回源校验

真实 PostgreSQL 测试发现：旧认识审阅使用生成时的全局 policy/source fence。新会话授予跨记录用途或其他无关授权变动后，旧证据会被保守判为过期，导致已经确认的认识无法用于对话。

本批没有修改旧的 KnowledgeAuthorizationSnapshotAdapter / KnowledgeEvidenceAuthorityAdapter，也没有把历史收据标为“仍然有效”。新增 `conversation-review-source-v1` 对话读取策略，针对本次已选范围重新建立证明：

1. 当前 MemoryClaim、ClaimVersion、DerivedObject 都存在于当前 Vault，且未删除。只选 system_to 为空的版本。
2. 复用已有裁定 reducer、激活规则和 allowed_uses，检查当前裁定、跨版本治理、撤回与抑制状态。只有可按用户提问使用的已确认/已纠正版本继续处理；有反例时保留 disputed 状态。
3. 当前证据必须仍可读取；核对 SourceDocument、SourceRevision、SourceFragment 身份、当前修订、片段哈希，以及 quote 起止和原话哈希。不能仅凭历史 evidence 的来源 ID 放行。
4. 逐来源检查 PASSIVE_QA 与 CROSS_RECORD_ANALYSIS。一般证据必须来自本会话已选日记；允许额外读取当前用户纠正版本关联的 CORRECTION 来源。其他未选日记不会借由认识证据进入上下文。
5. 高度敏感的认识或来源不进入该上下文。证据消失、引文损坏或来源越界时，整条认识排除，不通过删掉一个反例来保留其结论。
6. 模型网关仍在调用准备、实际派发和结果提交时执行既有全局 fence、成员版本、各用途供应商限制与敏感级别检查。这个读取策略只负责为本轮提供经重新验证的材料。

因此，无关 SEARCH 授权变化可以不影响对话对一条原文完好认识的使用；原文修改、撤权、删除仍然会阻止它进入上下文。

## 上下文、收据和旧回答

`application/conversation_memories.py` 负责有界回源和认识选择。最多检查 12 条关联认识，最多使用 6 条，每条最多 6 个证据链接，认识证据合计最多 12 个片段。结合最多 8 条日记和 10 轮问题，仍受既有模型片段与字符预算限制。这些上限会导致遗漏，不能宣称已读取用户全部认识。

关联检索包含历史版本到所选日记的关系，用来找到后来已经纠正的逻辑认识；发给模型的是当前版本，绝不把历史版本的陈述一并当成事实。

每条可用认识传递当前版本身份、用户裁定、声明、状态、不确定性、有效时间区间及实际证据片段身份。`ModelTaskContextSnapshot.additional_input_refs` 增加真实 DERIVED_OBJECT 引用，和 Source 引用一起进入运行收据，且必须属于同一 Vault。

加密回答信封中保存本轮参考认识和 memory_fingerprint。指纹覆盖版本、review_revision、治理事件、实际用途授权记录和本轮认识内容；读取及下一轮生成时重新计算。指纹不同则返回 `outdated`，不展示旧回答，也不向模型发送旧 assistant 历史。

认识创建、裁定、证据增删和来源证据失效服务现在先锁定 Vault，再操作认识行，与对话提交及来源变更保持一致的锁顺序，避免校验到提交之间插入新的用户裁定。既有 Knowledge 服务测试通过。

过时回答是隐藏和停止复用，并非物理抹除密文或备份；会话删除和用途撤回仍执行原有清理机制。旧版目录、备份及供应商侧副本不由此功能删除。

## 数据迁移与兼容

新增 Alembic `34cd56ef78ab`，在 `23bc45de67fa` 后增加：

```text
conversation.include_reviewed_memories BOOLEAN NOT NULL DEFAULT FALSE
```

已有会话默认不开启，避免扩大旧会话的材料范围；在新建会话时显式选择。POST `/v1/conversations` 新增可选字段 include_reviewed_memories，默认 false。GET 会话新增该设置、当前可用认识数量；回答中可包含本轮参考认识。

前置未发布迁移使用当前模型注册表建表，因此使用 ADD COLUMN IF NOT EXISTS 同时支持空库与已有数据库。支持离线 SQL 生成；真实迁移演练额外覆盖降到旧聊天 schema 后重新升级的路径。

没有修改版本号，本批更新源代码及 win-unpacked 本地验证构建，没有新建安装器或发布 GitHub Release。

## 已完成验证

- 全量后端测试：1123 passed，2 skipped；默认跳过的 PostgreSQL 检查由独立真实数据库流程执行。
- 严格 mypy：118 个源文件通过；Ruff、TypeScript、Vite 构建通过。
- 新增 12 项认识上下文回归：未裁定排除、当前确认、纠正版本、拒绝/暂缓/撤回、源级撤权、跨空间隔离、无关授权变化、原文修订、引文损坏、生成中裁定变化及旧回答不进入下一轮历史。
- 真实 PostgreSQL＋合成 HTTP 模型：确认后生成、原话引用、旧回答隐藏、纠正版本及纠正原话进入下一轮、幂等、取消、来源删除、会话删除、备份与重启通过。
- 实际 Electron＋合成 HTTP 模型：本地关键词选材预览、调整材料前置确认、已裁定认识展开、纠正后隐藏旧回答、继续提问、原文跳转、删除与 150% 缩放紧凑窗口通过。
- 打包后端＋真实 PostgreSQL：升级、降级至旧会话 schema 后再升级、非所有者 RLS 隔离，以及备份、恢复和重启通过。
- 最新 win-unpacked 空白启动通过：记录、认识、行动、会话和草稿均为空，无 API 密钥，AI 默认关闭。检查使用独立临时配置目录，没有清除用户已有数据。

测试均使用独立临时目录与合成日记，没有使用用户的真实模型密钥或真实日记。模拟模型会断言它收到当前确认/纠正版本，并检查旧 assistant 历史已被剔除；这些测试证明工程链路正确，不代表真实模型的解释质量已经通过评价。

复核命令：

```powershell
.venv/Scripts/python.exe -m pytest
.venv/Scripts/python.exe -m ruff check src tests alembic scripts
.venv/Scripts/python.exe -m mypy src/life_coach
cd apps/desktop
npm run build
node scripts/test-managed-runtime.mjs --integration --mock-ai
node scripts/test-managed-runtime.mjs --integration --postgres
npm run test:electron:chat
node scripts/test-electron-release.mjs --clean-only
```

## 仍然需要继续的部分

### 对话界面交互更新（2026-09-11）

对话页改为独立滚动消息区与固定底部输入框，宽窗口采用会话侧栏，窄窗口采用横向会话切换。日记列表默认折叠，选材与模型发送授权仍然保留。新增空会话引导、建议问题填入草稿、Enter 发送、Shift + Enter 换行以及中文输入法组合输入保护。阅读旧消息时不会因后台刷新强制跳到底部；发送新问题后跟随新消息。当前打开过的会话使用首个问题作为内存中的列表标题，没有另存明文标题。

TypeScript/Vite 构建和实际 Electron 回归通过，新增换行与键盘发送检查，并验证 980×760、150% 缩放下输入框在窗口内。已人工查看新建、空会话和消息界面截图。旧构建目录中的程序被占用，新验证构建改放 apps/desktop/release-chat-preview/win-unpacked，不自动关闭用户正在运行的程序。

- 召回目前是建立会话前的本地关键词预览，不是语义检索，也不是每轮自动扩大历史范围。会话分页、更多材料和更明确的覆盖说明仍需扩展。
- 不把被拒绝认识的正文放进“禁止事项提示词”。它们会被排除、使旧回答失效，但不能承诺模型永远不会从原始日记独立产生相似推断。
- 需要用时间变化、反例、用户修正等情境建立真实模型质量评价，检查是否提出具体、有依据、能帮助下一步思考的回答。
- P3 的事件、事项、长期趋势和记忆合并尚未实现。先完成质量评价与会话可用性，再扩大长期记忆的自动化范围。
