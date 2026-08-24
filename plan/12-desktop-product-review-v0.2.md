# Vistora Desktop v0.2 产品评审与重构说明

日期：2026-08-24  
评审范围：Windows 桌面端、现有 FastAPI 合约、Source → Memory → Verdict → Action 核心闭环

## 1. 评审结论

v0.1 已经证明 Electron 工程、Windows 打包、离线记录和后端健康检查可以工作，但它仍然更接近“可点击的设计稿”，还不是一个成熟的桌面产品。

最主要的问题不是单一视觉样式，而是产品层级：

1. 首屏以浏览记录为中心，没有承接用户“此刻想写一句”的最高频意图。
2. 固定三栏让候选认识长期占据注意力，不符合“按需出现、逐层展开”的信任原则。
3. 静态候选认识和行动在视觉上太像真实后端结果，示例与权威数据没有严格分层。
4. 前端只消费了 Source 创建和列表，遗漏修订、删除、Memory Inbox、Memory Detail 与 Verdict。
5. 后端默认应用只挂载 health；router factory 和领域模型已存在，但客户端无法直接知道哪些能力已经完成组合。
6. 连接状态只区分在线/离线，没有区分 live、ready、entries、memories、verdicts 和 actions。
7. 缺少搜索、快捷键、冲突恢复、删除影响、空状态、权限失效和真实错误分层。

## 2. v0.2 产品定位

Vistora 不是聊天机器人，也不是个人数据 Dashboard。它是一张安静的私人工作台：

> 先留下原话，再逐步整理；系统只提出候选，用户拥有最后解释权。

视觉借鉴 Claude Desktop 的不是品牌表面，而是以下结构原则：

- 输入优先：首屏首先承接表达，不先展示管理后台。
- 克制侧栏：导航和最近内容退后，主体保持大面积留白。
- 单列阅读：主要内容集中在稳定阅读宽度内，详情按需展开。
- 柔和层级：少用边框和同质卡片，以排版、留白、浅底色建立层级。
- 渐进披露：技术状态、证据、历史、授权和危险操作都在需要时出现。

Vistora 必须保留自己的差异：原话、系统候选、证据/反证、用户裁定和行动不能表现成同一种对话气泡。

## 3. 新信息架构

```text
今天
├─ 输入：写下此刻
├─ 待处理：同步、待回应认识、当前行动
└─ 最近记录

记录
├─ 搜索与筛选
├─ 记录流
└─ 详情 / 修订 / 删除影响

认识
├─ 待回应（Memory Inbox）
├─ 认识详情
├─ 支持证据 / 反证 / 上下文
└─ 确认 / 纠正 / 驳回 / 稍后

行动
├─ 候选
├─ 已接受
├─ 完成回顾
└─ 已撤销

设置
├─ 隐私与本地数据
├─ 后端连接
└─ 能力接入状态（开发/诊断信息）
```

## 4. 数据权威规则

| 对象 | 权威来源 | 离线策略 | UI 要求 |
|---|---|---|---|
| 草稿 | 本机 | 持续保存 | 明确“仅在此设备” |
| Source Record | 后端；未接通时本机队列 | 可新增、等待同步 | 不把本地状态伪装成已同步 |
| Candidate Memory | 后端 Memory API | 不在本地生成假的 AI 结论 | 示例必须显式标注 |
| Verdict | 后端 + ETag | 离线不伪造成功 | 冲突后刷新权威状态 |
| Action | 当前为本机产品原型 | 本机持久化 | 明确 Action API 尚未暴露 |
| Capability | `/health/capabilities` | 旧后端回退到安全探测 | 不再用单一“在线”覆盖所有能力 |

## 5. 后端能力审计

当前默认 ASGI 应用公开：

- `GET /health/live`
- `GET /health/ready`

已经实现、但需要 composition root 注入后才能挂载：

- Source：`POST/GET /v1/entries`、详情、PATCH 修订、DELETE tombstone；
- Memory：`GET /v1/memory-inbox`、详情、`POST /verdicts`；
- Evidence / Counterevidence / Authorization Snapshot / History / ETag 合约。

领域层已经存在、但没有 HTTP API：

- Action Candidate、Task、Experiment、If-Then Plan；
- 外部行动授权、claim-first 执行和 receipt；
- Model Run receipts 查询接口。

因此 v0.2 客户端应真实接入 Source 和 Memory 合约，Action 保持可用的本地原型，同时在设置页准确展示后端接入状态。

## 6. v0.2 验收标准

1. 启动后 3 秒内能直接写一条记录，不需要先理解导航。
2. 没有真实 Memory API 时，不展示未标注的 AI 候选。
3. 后端 live 但 entries 未挂载时，界面能清晰区分两者。
4. Source 支持创建、列表、详情、修订、删除和冲突提示。
5. Memory 支持 inbox、详情、证据数量和 Verdict；纠正必须带用户版本。
6. 键盘可完成新建、搜索、关闭弹层和保存。
7. 页面在 980–1600px 窗口宽度可用，正文在 200% 字体缩放时不溢出。
8. 生产构建、依赖审计、后端契约测试和 Windows EXE 冒烟测试全部通过。

