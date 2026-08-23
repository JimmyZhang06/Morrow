# 隐私、安全与运行保障

## 1. 安全姿态

人生记录比普通 SaaS 内容更敏感：一次跨用户检索、一条原文日志或一次删除残留都可能摧毁信任。安全目标按以下顺序排列：

1. 不跨 vault 泄露；
2. 不在未经同意的目的下使用；
3. 不让供应商、日志、任务历史无界复制内容；
4. 用户可看、可改、可撤回、可导出、可删除；
5. 发生错误时可定位影响范围且不需要读取更多私密内容。

## 2. 威胁模型

首版至少覆盖：

- IDOR、RLS 遗漏、缓存键遗漏 vault 导致跨用户泄露；
- 数据库、对象存储、备份或开发快照泄露；
- 员工/运维越权查看；
- 日志、APM、错误追踪记录 prompt、引文或 token；
- 模型供应商保留或训练使用；
- 日记/导入网页中的 prompt injection；
- 恶意附件、OCR/解析器漏洞；
- OAuth token 泄露或集成产生意外外部副作用；
- 删除后 embedding、图边、缓存、草稿或备份残留；
- 系统主动再呈现创伤/私密内容造成伤害；
- 账号被盗后批量导出全部人生数据。

## 3. 数据分级与目的限制

```text
normal             普通笔记与产品元数据
sensitive          关系、工作、位置、财务等个人内容
highly_sensitive   健康、创伤、性、违法经历、危机表达等
```

分级用于**减少使用范围**，不能用于给用户打隐性风险分。每项数据还需记录允许目的：搜索、被动问答、跨记录分析、主动提醒、回忆录、第三方集成。

默认策略：

- 新记录可保存；长期推断与主动再呈现分别授权。
- 高敏内容不进入通知文案，不用于营销或推荐。
- 第三方集成永不接收心理假设、临床风险信号或原始引文，除非用户对当前 payload 明确选择。
- 测试和分析使用合成或去标识数据；生产原文不进入通用数据仓库。

## 4. 加密与密钥

### 4.1 基线

- 客户端到 API、服务间和数据库连接全部 TLS。
- 数据库磁盘、对象存储、快照与备份启用平台静态加密。
- 使用分层 envelope encryption：KMS 根密钥包裹每个 vault 的 KEK，vault KEK 再包裹每个对象/每个 revision 的 CEK；原始附件、导出文件和高敏正文用各自 CEK 加密。
- 删除单条来源时销毁其 CEK 并继续物理擦除正文与派生投影；销毁 vault KEK 只适用于账号/vault 级 crypto-shredding，不能拿它承诺单条日记已从所有索引和备份消失。
- OAuth/供应商密钥进入 Secret Manager，不写 DB 普通列或日志。

### 4.2 必须诚实说明的边界

服务器端全文/语义检索需要受限 Worker 在内存中解密获授权片段，并把 service-readable 词项/`tsvector` 与 embedding 写入独立 `search_projection`。因此本方案是**受控服务器可处理的数据加密架构**，不是真正 E2EE。

搜索索引本身也是敏感数据：

- 使用单独 schema/角色、严格 RLS 与最短保留；
- embedding 视同个人数据，不公开、不跨用户训练；
- 高敏记录可选择不建云端索引，只保留加密原文；
- 撤回索引权限后先从查询视图排除，再清理词项、向量和缓存。

若未来承诺 E2EE，embedding、检索和尽可能多的推理必须迁到客户端/可信执行环境，并重新设计同步；不能仅更换营销文案。

## 5. 多租户隔离

- API 根据认证上下文推导 `vault_id`，拒绝客户端任意覆盖。
- PostgreSQL 所有业务表启用并 `FORCE ROW LEVEL SECURITY`、默认 deny；API/Processor 业务角色不是表 owner、无 `BYPASSRLS`，只在事务内 `SET LOCAL app.vault_id`。全局 Dispatcher 只能读无正文 job 元数据，migration 与 break-glass 使用独立角色。
- 唯一索引、缓存 key、对象 key、向量查询和图 `group_id` 全部包含 vault。
- 管理员工具通过短期、带理由、双人审批的 break-glass 流程，默认只能看元数据。
- 自动化测试生成两个 vault 的相似 canary，所有 API/FTS/vector/export 均验证零交叉。

不要把应用层 `WHERE user_id = ?` 当作唯一隔离措施。

## 6. 模型与供应商治理

供应商接入前建立 data-processing registry：

```text
provider / model
processing region
training use
retention duration
zero-retention eligibility
subprocessors
supported task and max data class
contract/doc evidence and review date
```

运行时 Model Gateway：

- 从 consent 与 data class 计算允许供应商；
- 只发送完成该任务所需的最小片段；
- 不使用公共 consumer chat 接口处理生产数据；
- 不把用户 ID、真实姓名放进供应商 metadata；
- 禁止 provider fallback 绕过地域、留存或敏感等级政策；
- 对响应做 schema、引用和注入后检查。

撤回同意会通过 `policy_epoch` 阻止后续调用并丢弃在途结果，但已经发送给外部供应商的数据不能由本系统“收回”。系统只能依据供应商删除 API、零留存合同和既定保留期跟踪其状态，并向用户如实展示 `provider_deletion_status/retention_until`。

## 7. 日志与可观测性

### 7.1 日志禁止项

普通应用日志、trace、job dashboard 和 error tracking 禁止记录：

- 原始正文、引文、模型 prompt/response；
- 音频/附件下载 URL；
- access/refresh token；
- embedding；
- 临床风险细节或安全对话内容。

记录资源 ID、模型任务类型、版本、时延、token 数、错误类别和 trace ID 即可。必要的加密模型产物放专用受限存储，短期保留。

### 7.2 指标

业务可靠性：

- capture success / latency；
- job queue age、retry、dead-letter；
- extraction/verification/index latency；
- provider error、timeout、cost；
- deletion completion age；
- export completion；
- cross-vault policy denial。

质量与信任：

- citation coverage；
- candidate confirm/correct/reject/snooze（只看聚合，不优化成讨好确认率）；
- contradiction retrieval；
- stale memory rate；
- “不像我/无依据/太私人”反馈；
- 主动通知关闭率和不适反馈。

不把情绪内容、日记主题或脆弱性用作增长分群。

## 8. 初始 SLO 与告警

这些是开发目标，需上线后按负载校准：

| 能力 | 初始目标 |
|---|---|
| 文本记录写入 | 月可用性 99.9%；P95 < 300ms（不含附件） |
| 原文读取/编辑 | 月可用性 99.9% |
| AI 摄取 | 95% 文本在 5 分钟内 ready；失败可重试且不丢原文 |
| Coach 首 token | P95 < 3 秒，供应商允许时 |
| 隐藏删除内容 | API 接受请求后立即不可检索 |
| 在线存储物理删除 | 通常 24 小时内；异常可见并告警 |
| 导出 | 常规 vault 24 小时内完成 |
| 跨 vault 泄漏 | 0；任何命中按最高级事故处理 |

不要为达到时延而跳过安全网关、权限过滤或证据校验。

## 9. 数据保留、导出与删除

### 9.1 保留

- Source：用户控制，直至删除；导入临时文件确认后尽快清理。
- Model raw output：仅调试需要的最短期限，加密且权限受限；稳定派生对象保留结构化结果。
- ContextPack：短期保留或按项目冻结；普通问答包尽快过期。
- Job payload/log：只含 ID，按运维需求有限保留。
- 安全事件：与普通生命模型分区，只保留最小审计元数据和明确期限。
- 备份：声明可理解的保留窗口；任何备份恢复必须先重放不可变删除账本并验证 canary，随后才能开放业务读取。

### 9.2 导出

提供：

- 原始记录 Markdown/JSON + 附件；
- 版本与时间线；
- 用户确认的记忆/目标/实验；
- AI 候选、证据链接和裁定；
- 回忆录草稿；
- 设置、同意和有限审计记录。

导出包加密、下载链接一次性且短期有效；高风险批量导出要求近期重新认证，并通知现有设备。

### 9.3 删除验证

删除任务记录每个 sink 的状态：

```text
postgres_source
postgres_derived
fts
vector
object_store
cache
graph_projection
model_artifacts
integration_copy
backup_tombstone
```

删除状态分三类，不能用一个 `completed` 混淆：

```text
online_purged_at              在线 DB、对象、索引、缓存和图投影已不可访问并物理清理
provider_deletion_status      requested | confirmed | retention_only | unsupported
provider_retention_until
backup_expires_at             删除数据可能仍在不可在线查询的历史备份中，何时自然过期
restore_replay_verified_at    最近一次恢复演练已先重放删除账本
```

在线清除完成后可将请求标为 `online_purged`；第三方和备份到期分别继续跟踪，异常进入明确人工队列。用户看到各层真实边界和预计时间，不用模糊的“我们已收到请求”或“全部已删除”掩盖不可变备份与供应商保留期。

## 10. 应用安全

- OIDC/OAuth 采用 PKCE；session cookie `HttpOnly/Secure/SameSite`；敏感操作重认证。
- CSRF、CORS、CSP、速率限制、上传类型/大小检查、恶意文件扫描。
- 解析 PDF/图片的进程沙箱化，限制 CPU、内存、网络与文件访问。
- 依赖锁定、SBOM、许可证扫描、secret scanning 和镜像签名。
- 所有外部 URL 获取防 SSRF；重定向和 DNS rebinding 受控。
- 生成的 Markdown/HTML 严格清洗，防存储型 XSS。
- Prompt injection 被当作应用安全问题，而非只靠 system prompt。
- 外部 tool 默认 deny；允许列表、参数 schema、目的检查、最终确认与幂等。

## 11. 事故响应

最高级事故包括跨 vault 读取、未授权模型供应商处理、批量导出滥用、删除内容重新出现。预案应支持：

1. 关闭某模型、检索索引或连接器的 feature flag；
2. 通过 trace/resource ID 确定影响对象，不展开内容；
3. 吊销 session、OAuth token、DEK；
4. 停止主动通知与后台反思，但保持记录/导出通道；
5. 通知受影响用户并提供具体补救；
6. 用合成 canary 回归修复；
7. 更新 threat model、测试与保留策略。

## 12. 上线安全门槛

在邀请真实用户上传私人日记前，至少完成：

- RLS 与跨 vault 自动化测试；
- 日志/trace 内容扫描；
- 删除全链路 canary；
- 模型供应商数据处理审查；
- 导出与账号接管保护；
- prompt injection 与恶意附件测试；
- 高敏内容再呈现开关；
- 安全回应的人工情景测试；
- 外部渗透测试或独立安全评审。
