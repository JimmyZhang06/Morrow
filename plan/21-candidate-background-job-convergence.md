# 候选认识后台任务收敛记录

日期：2026-08-25

## 本轮范围

只收敛既有闭环中的模型调用阶段，不扩展回忆录、人生主线或外部 Todo/Calendar：

`记录 → 后台生成候选认识 → 查看真实依据 → 用户裁定 → 可撤销小行动`

## 已实现

- `POST /v1/entries/{entry_id}/candidate-insights` 改为持久化 Job 入队，通常返回 `202`，HTTP 请求不再等待 StepFun。
- Job 保存发起者 principal、membership generation、记录 revision 与 Vault fence；不保存 Bearer Token、原文、prompt 或模型原始输出。
- 单并发 worker 通过窄 `SECURITY DEFINER` 函数只领取 `candidate_insight.generate`，不能借此浏览其他 Vault 内容。
- worker 在执行前重新检查 principal-vault membership generation、当前记录 revision 与 Source authority。
- 模型调用仍只经过 `GovernedModelGateway`；ModelRun receipt 是推理结果的权威状态，Job 是可恢复的调度账本。
- 新增 Job 查询与取消 API，向前端公开安全阶段：排队、读取原文、核对依据、模型生成、成功、失败、结果未知、取消中、已取消。
- worker 在运行期间延长 lease；取消在 dispatch 后把 ModelRun 记为 `unknown`，且不保存 Artifact。
- 新增过期 Job 与 ModelRun receipt 的终态对账，避免“模型已成功但 Job 仍 running”或进程中断后重复调用模型。
- 前端记录详情显示真实进度条、取消、失败与重试；任务技术状态保存在本机，刷新后可继续轮询已知 Job。

## 真实验证证据

- StepFun 受控 canary：Job 从 `queued → processing/generating → succeeded`，生成 1 个 ModelRunArtifact 与 1 条 EvidenceLink。
- 浏览器取消 canary：页面显示 `等待开始 → 正在形成候选认识 → 已取消`；数据库结果为 Job `canceled`、ModelRun `unknown`、Artifact `0`。
- 竞态注入：发现 SQLAlchemy UPDATE bind 参数与列名冲突，已改为 operation-prefixed 参数并增加 PostgreSQL 编译回归测试。
- 租约恢复演练：已成功把历史 `ModelRun=succeeded / Job=running` 对账为 `Job=done`。
- 一次性 PostgreSQL staging：`upgrade → downgrade → upgrade` 与非 owner runtime 边界测试通过。

## 尚未解决

1. **跨设备任务发现（P1）**：当前前端只恢复本机已知 Job ID；后端尚无“按 entry 查询最近候选任务”的列表接口。刷新本机可继续，换设备不能自动发现正在运行的任务。
2. **多进程部署（P1）**：worker 目前随 FastAPI 进程启动。数据库领取与 lease 可保证不会重复领取，但正式水平扩容前应拆为独立 worker deployment，并加入队列深度、lease age、dead/waiting 告警。
3. **结果未知的人工处置（P1）**：`unknown` 已禁止自动重试并允许用户用新幂等键重试，但还没有专用 reconcile 后台流程查询供应商结果。
4. **进度精度（P2）**：当前进度来自可验证的持久化阶段，不是假动画，但供应商调用内部仍只能显示单一 `generating=65%`，StepFun API 没有可用的 token 级任务进度。
5. **测试记录清理（P2）**：本轮按授权保留了带 `[受控测试]` / `[受控取消测试]` 前缀的记录与审计账本；没有执行不可逆删除。

## 下一步顺序

1. 增加按 entry 查询最近 Job 的 API，完成跨设备/清缓存恢复。
2. 把 worker 拆成独立进程入口并增加 metrics/readiness。
3. 为 `waiting/unknown` 建立受控 reconcile 流程。
4. 稳定后再扩展回忆录章节、人生主线与外部 Todo/Calendar。
