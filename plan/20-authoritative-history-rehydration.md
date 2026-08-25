# 认识与行动历史权威水合（2026-08-25）

## 目标

解决 30 天可用性 canary 中的两个 P0：

1. 已认可、已纠正和未采用的认识在刷新后无法回看。
2. 行动历史依赖浏览器本地状态，无法从服务端恢复。

本轮未扩展回忆录、人生主线或外部 Todo/Calendar。

## 已完成

### 认识历史

- 新增 `GET /v1/memories?limit=&cursor=`，返回 Vault 范围内的当前认识及权威用户裁定。
- 保留 `GET /v1/memory-inbox` 的原有“只返回待判断项”语义，避免破坏生成与审阅职责。
- 认识历史包含 pending、confirm、correct、reject、snooze 和 retract 状态。
- 用户纠正生成的新版本现在显式呈现 `current_verdict=correct`，刷新后不会误回待判断。
- 删除的 Claim/DerivedObject 仍被 Vault 过滤和 tombstone 过滤保护。
- 前端三个 tab 改为使用服务端完整历史，不再把 inbox 误当作历史接口。

### 行动历史

- 新增 `GET /v1/actions?limit=&cursor=`，返回 Vault 范围内的行动当前状态。
- 列表项携带每个行动自己的 ETag，刷新后仍可执行受并发保护的 accept/complete/revoke。
- 前端启动和每分钟刷新时从服务端重新水合远端行动；纯本地草稿仍保留，不会被服务端结果覆盖。
- 水合时通过 `memory_id` 恢复来源认识文案。
- 没有可用证据的旧认识不再展示“创建行动”入口，最终资格判断仍由后端执行。

## 真实环境证据

使用现有月度数据库和非 owner runtime API：

- `GET /v1/memories` 返回 10 条当前认识：6 confirm、1 correct、1 reject、1 snooze、1 pending。
- `GET /v1/actions` 返回 3 条行动：2 completed、1 revoked；所有列表项都有 ETag。
- 页面刷新后，“已认可”显示 7 条，其中纠正版本显示“已按你的理解修正”。
- 页面刷新后，“未采用”显示 1 条驳回认识。
- 页面刷新后，“历史”显示 3 条服务端行动，并恢复各自来源认识。
- 用户在测试期间新增的数据被保留，没有再次重置数据库。

## 验证

- 新增 Memory service、Memory API、Action service、Action API 的 Vault、分页、状态和 ETag 回归测试。
- 全量后端测试通过，2 个环境型测试默认跳过。
- Alembic staging rehearsal 和 non-owner PostgreSQL 权限测试单独通过。
- Ruff、mypy（98 source files）、前端 TypeScript typecheck 和 production build 通过。
- 浏览器跨刷新实测通过。

## 剩余收敛项

两个 P0 已关闭。下一轮最高优先级变为：

1. 将批量候选认识生成改为限流后台 job，并显示真实进度、失败和取消状态。
2. 给 staging reset 增加环境 epoch/replay fence，避免旧客户端向新数据库重放缓存记录。
3. 证据卡使用 Source `captured_at` 显示原始记录时间，将认识生成时间单独标注。

