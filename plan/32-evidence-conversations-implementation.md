# 有依据的持续对话：第三批实施记录

日期：2026-09-11。接续 [技术实现书](29-long-term-memory-chat-technical-spec.md) 与 [分段索引、后台补建](31-passage-index-background-jobs.md)。

> 本文记录第三批交付时的状态；后续选材与认识纠正接入见 [第四批实施记录](33-recall-reviewed-memory-implementation.md)。

## 交付范围

本次实现 P2 的第一批：用户显式选择日记，建立保存在本机的多轮 AI 对话，回答附带可核对原话。原有记录、认识、尝试、主线功能保留。

这不是完整的长期记忆系统。当前不自动遍历历史、不使用向量召回、不把 AI 回答提取为用户事实，也没有把用户已纠正的认识自动放入对话。后面的“自动召回＋吸收纠正”仍需要继续实施和评价。

## 用户可以怎样使用

1. 在桌面设置中配置并开启自己的在线模型。
2. 进入“对话”，选择最多 8 条已同步日记。也可以不选日记，从当前问题开始。
3. 勾选本次跨记录对话说明，再建立会话。本会话材料范围固定；修改范围需新建会话。
4. 提问并继续追问，等待真实后台任务完成。回答包含范围/不确定性说明，引用可点回原文。
5. 随时停止生成或删除会话。问题和回答在本机保存，重新打开可继续读取。

首版每会话最多 10 轮。输入问题最多 3000 字符，回答最多 6000 字符；原始材料合计最多 24000 字符，包含会话历史的上下文另受 32000 字符预算限制。预算是字符上限，并非精确 token 计费。超限任务会失败，不会静默截断成另一份证据。

## 数据与权限设计

迁移 `23bc45de67fa`，前置版本 `12ab34cd56ef`：

| 存储 | 用途 |
| --- | --- |
| `conversation` | 空间内会话身份、时间和删除标记 |
| `conversation_material` | 会话固定选定的 SourceFragment 身份与顺序 |
| `conversation_turn` | 幂等请求、用户原话来源、任务状态、成员版本、授权/来源版本、模型绑定、加密回答 |
| `model_run_artifact.conversation_turn_id` | 运行收据到回答的类型化指针；由形状 CHECK、复合 FK、唯一约束保护 |

用户问题保存为 `SourceType.CONVERSATION` 的加密 SourceRevision/SourceFragment。普通记录列表排除这些问题，避免聊天淹没日记列表。AI 回答及引用原话在同一加密信封保存；明文引用清单只保存来源身份和位置。回答不是 Source，不自动成为 MemoryClaim。

会话与问题均属于当前 Vault，三张新表注册到现有 PostgreSQL RLS。所有入口复用现有身份及成员版本检查，不从前端接收任意提示词、provider 或 context manifest。

首次建立会话需要 `PASSIVE_QA`，并由用户勾选跨记录说明授予 `CROSS_RECORD_ANALYSIS`；沿用既有问答用途的供应商限制。模型网关分别检查两个用途，包括源级 opt-out 和各自供应商策略。上下文组装在解密前检查两个用途；真正调用前和结果提交时再次检查权限和依赖。此版跨记录授权仍是 Vault 级用途授权，实际发送材料由会话固定选择约束。

## 生成链路

1. POST 创建用户问题 Source 和 `queued` turn；同会话同 request_id 重放返回同一 turn。换正文重用 request_id 被拒绝；同会话只允许一个活跃 turn。
2. `life_coach_private.claim_conversation_turn()` 领取任务，检查空间、身份、成员版本和会话是否有效。函数不向 PUBLIC 开放执行。
3. `ConversationAuthority` 验证实际当前 Source 身份、hash、授权和预算，并将当前问题 SourceRevision 作为真实 context anchor。历史 AI 回答仅为可错的上下文，不作为新证据。
4. 通过原有 `GovernedModelRuntime` 执行准备、供应商调用、结果提交三个阶段；生成已有 model_run 收据和新增 CONVERSATION artifact。
5. 结果持久化前，检查每个引用 ID 属于授权片段，quote 是原文中完全一致的连续文本；不匹配则整次回答拒绝入库。
6. 完成后桌面轮询显示完整回答，不用假 token 动画冒充流式响应。

引用验证只证明引文真实且在允许范围内，不证明模型推断正确、因果关系成立或引用足以支持整段回答。空引用允许用于追问等非事实性回答；目前没有逐项论断自动覆盖校验。

任务绑定排队时的 provider/model/revision；绑定改变不转发到另一服务。运行租约为 180 秒；失去确认的运行转为 `unknown`，不自动重发。停止生成阻止迟到答案提交，但无法保证供应商侧已经停止计算或计费。

## 修订、撤权和删除传播

- 日记追加修订或删除：清空相关会话回答密文和引用、取消相关 turn；旧固定片段不再是当前版本，读取与继续生成均受阻。
- 撤回问答或跨记录用途：在中央 `record_consent` 路径清空相应回答并取消任务。重新授予权限不复活已清空的回答。
- 删除会话：清空回答，对用户问题 Source 执行既有逻辑删除，隐藏会话。
- Vault 的全局 policy/source fence 继续使用，因此无关材料变动也可能保守地使运行失效；此版没有放宽旧网关的保护。
- 逻辑删除不抹掉数据库备份、旧版本目录或已发送给供应商的数据；本次没有改动备份保留机制。

## 当前 API

使用既有 Authorization 和 X-Vault-ID，响应不缓存。

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| POST | `/v1/conversations` | entry_ids、allow_history；创建返回 201 |
| GET | `/v1/conversations` | 最近 100 个会话摘要、模型 ready 状态 |
| GET | `/v1/conversations/{id}` | 问题、可用回答、引用、blocked 状态及轮数限制 |
| POST | `/v1/conversations/{id}/turns` | request_id、question；排队返回 202 |
| POST | `/v1/conversations/{id}/cancel` | 取消当前活跃轮次，204 空响应 |
| DELETE | `/v1/conversations/{id}` | 逻辑删除，204 空响应 |

健康能力标记增加 `conversations` / `conversation_generation`。无模型时仍提供本地会话读取/删除入口。接口尚未实现设计稿里的会话/消息游标分页、范围编辑与独立 manifest 查询；当前限额避免无限增长上下文，超过最近 100 个的会话需要后续分页支持。

## 验证

- 后端全量测试：1111 passed / 2 skipped，包含撤权重授权、已完成回答修订、解密前 opt-out 回归。默认跳过的 PostgreSQL 项目另由真实运行时集成流程执行。
- Ruff 与严格 mypy 检查通过，React TypeScript 与 Vite 构建通过。
- 真实 PostgreSQL：迁移升级、非 owner 角色隔离、既有搜索及备份恢复、重启流程通过。
- 真实数据库＋合成 HTTP 模型：生成、连续追问、引用精确匹配、幂等重放、重启后恢复、虚构引用拒绝、运行中取消且迟到回答不入库、来源删除传播、会话删除通过。
- Electron 实际界面＋合成 HTTP 模型：初始空白、模型前置条件、显式选择/同意、两轮回答、引用跳回日记、删除、150% 缩放下 980×760 窗口通过。
- 打包后的 Electron：全新版本配置内无记录、认识、行动、会话或草稿，模型密钥为空、AI 关闭；Windows 密钥保护、多模型设置、备份恢复和重启通过。
- 所有数据均在独立临时目录，模型响应由 `compatible_backend_fixture.py` 合成，不读取真实日记或使用真实 API Key。这验证工程链路，不代表真实模型回答质量已达标。

开发检查：

```powershell
.venv/Scripts/python.exe -m pytest
.venv/Scripts/python.exe -m ruff check src tests
.venv/Scripts/python.exe -m mypy src/life_coach
cd apps/desktop
npm run build
node scripts/test-managed-runtime.mjs --integration --source --postgres
node scripts/test-managed-runtime.mjs --integration --mock-ai
npm run test:electron:chat
```

数据库运行时构建会解包 PostgreSQL DLL，不要与使用该目录的集成测试并行运行。

## 下一步

1. 在显式授权范围内把 P1 搜索接入对话召回，展示命中材料和覆盖范围；支持更完整的会话分页与材料清单。
2. 将已确认/纠正/拒绝的认识按版本加入上下文，增加纠正后不复述旧结论的回归；避免仅靠堆积聊天记录维持记忆。
3. 用具有时间变化、反例、来源删除和模糊问题的合成日记集评价：相比直接问通用模型，是否能提出更具体且有依据的解释；不要以“能输出文字”作为质量验收。
4. 再进入 P3 的事件/事项及长期记忆更新，把结论的时间范围和待验证状态显式呈现。

本次更新开发代码和 `win-unpacked` 本地验证构建，不改变版本号，不生成新的安装器或发布 GitHub Release。
