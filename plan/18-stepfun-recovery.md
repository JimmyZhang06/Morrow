# StepFun 连接与安全重试修复（2026-08-25）

## 故障结论

一次真实候选生成在连接阶段记录为 `transport_unexpected`，运行被保守地标记为 `unknown`。桌面端随后复用同一幂等键，因此只能持续得到 409，表现为“发现线索”无法恢复。

## 已修复

- 新增后端专用 `APP_STEPFUN_PROXY_URL`，仅接受显式 HTTP(S) 代理 origin；代理凭据使用 `SecretStr`，不进入日志或对象表示。
- StepFun adapter 将 TCP、代理和连接建立失败归类为 `transport_connect_failed`，并标记为尚未发送远程推理请求。
- Model Gateway 与 Runtime 将该状态落为已知 `failed`，不再落为 `unknown`；API 返回 `503 MODEL_PROVIDER_UNAVAILABLE` 与安全的 `Retry-After`。
- 桌面端只在后端明确给出连接前失败或已知 failed 时清理旧幂等键；下一次用户点击使用新键。
- 真正的 `unknown` 不会自动重试。界面先显示“重新尝试”，再次点击时明确告知可能产生重复结果并要求用户确认。

## 验证

- MockTransport 覆盖连接失败、密钥/正文不泄露和异常上下文清理。
- Gateway/Runtime 覆盖 `failed-before-dispatch` 与 `outcome-unknown` 的不同状态迁移。
- 通用候选 API 与 entry 候选 API 均覆盖安全 503 响应。
- 设置测试覆盖代理隐藏和 URL 约束。
- 重启本地栈后，使用新建虚拟记录完成一次真实 StepFun 候选生成，返回 200。

## 运维说明

默认继续直连且不继承系统代理。只有部署网络无法直连 `api.stepfun.com` 时，才在后端环境或 Secret Manager 配置 `APP_STEPFUN_PROXY_URL`；禁止写入前端配置、Git 或日志。
