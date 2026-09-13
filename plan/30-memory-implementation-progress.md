# Morrow 长期记忆改造实施记录

日期：2026-09-10。对应技术方案：`29-long-term-memory-chat-technical-spec.md`。

> 本文保留第一批验收记录。2026-09-11 的分段与后台补建更新见 [第二批记录](31-passage-index-background-jobs.md)，其中部分内容已替代本文的初始限制。

## 本批交付

本批实现 P1 的本地词法检索基础链路，修复其依赖的搜索撤权和重建问题。没有新增在线模型调用、聊天生成或自动长期记忆提取；旧的认识、尝试、叙事接口继续使用原来的执行与授权边界。

| 部分 | 实际行为 |
| --- | --- |
| 搜索授权 | 桌面明确开启 SEARCH，默认关闭；独立于在线 AI 设置 |
| 新记录与修订 | 写入事务内建立当前片段的词法索引；不请求供应商 |
| 旧记录 | 用户点击补建，每批最多处理 50 条；已完成的索引不会反复重建 |
| 检索读取 | 验证空间、当前修订、当前来源授权和敏感级别；命中后解密回源验证 hash 与关键词 |
| 无关新增 | 新写一篇日记或授予另一用途，不再让其他有效日记的本地索引整体失效 |
| 删除与撤权 | 删除的来源、旧修订和被排除来源不会返回；关闭搜索清除索引载荷，原日记保留 |
| 重新授权 | 空索引可以重新建立；真正来源删除的 tombstone 仍不可逆 |
| 桌面 | 记录页提供开启、搜索、补建、关闭与原文入口；未同步记录提示不在检索范围内 |
| 兼容性 | `local_search` capability 控制入口；旧后端不出现不可用按钮；无数据库 schema 变更 |

## 修复的实际缺陷

1. 旧投影读取依赖整个 Vault 的 generation，容易因无关日记写入而排除仍有效的索引。新增读路径以当前修订和即时授权判断有效性；旧模型执行栅栏未被放宽。
2. 搜索撤权把投影设为删除状态，真实 PostgreSQL 的 RLS 和不可恢复 tombstone 约束使撤权或再次授权重建失败。现在撤权清空载荷及策略，保留可重建的空行；来源删除仍走原来的不可恢复删除流程。
3. 初始 Vault 的 policy epoch 为 0，授权接口原校验不能假定从 1 开始。请求接受 0，并返回数据库实际产生的 consent epoch。
4. 搜索可能命中列表尚未加载的旧记录。详情入口现在允许按授权 API 读取此 ID 并补入列表，不再仅依赖已加载记录。
5. 桌面记录先本机保存再同步；搜索页面明确提示未同步数量，并在同步状态变化时丢弃旧结果。

## 主要代码

- `src/life_coach/application/local_search.py`：本地索引、批量授权读取、当前版本检索、回源片段。
- `src/life_coach/api/local_search_composition.py`：认证、状态、SEARCH 授权、查询和补建接口。
- `src/life_coach/application/source_entries.py`：保存和修订后的事务内索引。
- `src/life_coach/modules/consent/service.py`：批量来源授权，复用单条授权 reducer。
- `src/life_coach/modules/sources/service.py`：撤权清空索引与来源 tombstone 区分。
- `apps/desktop/src/LocalSearchPanel.tsx`：实际桌面入口与覆盖提示。

所有接口要求现有 Bearer 认证、X-Vault-ID 和空间成员权限，响应使用 no-store。

| 方法 | 路径 | 请求 |
| --- | --- | --- |
| GET | `/v1/local-search/status` | 无正文 |
| POST | `/v1/local-search/permission` | `enabled`、`expected_policy_epoch` |
| POST | `/v1/local-search/query` | `query`（1–500 字符）、`limit`（1–50） |
| POST | `/v1/local-search/rebuild` | 无正文；最多新建 50 条 |

查询返回 `items/scanned/indexed/truncated/rebuilt/pending/mode`。命中包含原记录 ID、修订号、片段 ID、摘录及 Python Unicode 字符偏移。未来前端如按偏移定位，需显式转换 JavaScript UTF-16 偏移。

## 验证

- 后端最终完整回归：**1083 passed、2 skipped**，21.44 秒；另有一条现有 Starlette/httpx 弃用提示。
- 新增真实加密来源测试覆盖授权默认关闭、源级 opt-out、跨空间、编辑、删除、撤权、再授权、敏感记录排除、批量授权一致性、补建续批和截断提示。
- 新增 API 契约测试覆盖认证、初始 epoch 0、冲突与幂等授权。
- 真实内置 PostgreSQL + 源码后端联调通过：授权、补建、增量修订、无关 generation、关闭/重开、加密备份、恢复与重启。受限角色和迁移演练亦通过。
- Python 静态类型检查与前端 TypeScript/Vite 构建通过。
- 实际 Electron 开启、补建、搜索、关闭已通过；150% 搜索窗口无横向溢出，现有六页在 100%/150%/200% 的常规与紧凑窗口字号检查通过。截图仅包含合成测试记录，位于 `.data/release-evidence/local-search/`。
- 实际 `win-unpacked` 空白启动检查通过：记录、认识、行动与草稿为空，没有模型密钥，AI 默认关闭；旧目录未自动导入。
- 桌面专项额外创建不在前端列表中的历史记录，验证搜索命中后可以通过详情接口打开原文；测试通过。
- 测试未使用用户真实模型密钥，未请求真实在线供应商。

复现新增专项：

```powershell
.venv/Scripts/python.exe -m pytest tests/application/test_local_search.py tests/api/test_local_search_api.py -p no:cacheprovider
cd apps/desktop
node scripts/test-managed-runtime.mjs --integration --source
npm run test:electron:search
```

Electron 测试前必须先构建前端、Python 运行时和 `win-unpacked`，否则检验的是旧产物。`--source` 使用源码后端与内置真实 PostgreSQL，不等于已测试安装器。

## 当前限制与后续门槛

本批复用现有整篇 SourceFragment 和 JSON 词法投影，不等于技术方案 P1 已全部完成：

- 每次最多检查最近 1000 条当前片段，明确返回 `truncated`；更早材料暂不覆盖。
- 单条索引正文上限 100,000 字符，高度敏感来源和会话材料不进入本索引。
- 索引词可在本机数据库读取；开启时已在界面说明，原文继续按原有方式保护。
- 当前保存同步建索引，补建手动分批。没有持久后台索引任务、chunk 表、语义 embedding，也没有 5000 chunk 性能达标结论。
- 关键词命中不能判断语义、时间变化或反证，不能当作长期记忆分析质量已经提升的证据。

后续按原方案依赖顺序执行：补齐 P1 的 chunk 与持久任务及性能验收；P2 增加 saved 会话、turn、manifest、引用、reply artifact、多用途授权与生成任务；P3 接入可纠正的记忆、事件、事项和反例；最后完成 P4 删除传播、备份恢复与发布验收。P2 不能直接绕过现有模型网关调用供应商。

当前仅生成开发验证用 `win-unpacked`，没有发布新版本安装器，没有推送 GitHub。既有安装包不能据此当作包含本批改造。用户的真实数据目录没有被清空；测试使用独立临时资料目录。
