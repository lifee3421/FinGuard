# Research Studio UI API Mapping

状态：第一阶段 Adapter 准备，适用于 FROZEN v1；不迁移或覆盖 `FinGuard_Research_Studio_v05_Settings.html`。v0.6 视觉稿接入前，本文件是 UI 与后端真实能力的边界说明。

## 真实 ApplicationService 路由

| UI 操作 | HTTP 请求 | 服务调用 | UI 使用的真实字段 | 错误处理 |
|---|---|---|---|---|
| 创建任务 | `POST /api/tasks` body `{prompt,data_mode,allow_paper_order_proposals}` | `start_task(prompt, data_mode, allow_paper_order_proposals)` | `run_id`, `status`, `step`, `data_mode`, `allow_paper_order_proposals`, `version`, `pending_proposal`, `final_answer`, `error` | `422` 表示输入无效；网络错误显示明确错误，不创建本地假 run |
| 查询任务 | `GET /api/tasks/{run_id}` | `get_task(run_id)` | 同上；`run_id` 是轮询隔离键 | 非 2xx 转为 `ApiError`；不使用前端旧快照覆盖服务端状态 |
| 增量 Trace | `GET /api/tasks/{run_id}/events?after_seq=N` | `get_events(run_id, after_seq=N)` | `seq`, `event_type`, `step`, `tool_call_id`, `status`, `error_code`, `elapsed_ms`, `mode`, `created_at`（服务实际返回什么就显示什么） | 轮询记录服务端 seq；切换 run 会停止旧轮询，旧响应不会写入新 run |
| 批准/拒绝模拟订单 | `POST /api/proposals/{proposal_id}/decision` body `{run_id,decision,expected_version}` | `decide_proposal(run_id, proposal_id, decision, expected_version)` | `proposal_id`, `decision`, `proposal_status`, `order_status`, `task_status`, `receipt`, `version` | `VERSION_CONFLICT`/HTTP 409 显示版本冲突并重新读取任务；`PRICE_GAP_EXCEEDED` 或其他 BLOCKED 只显示后端结果，不改账户 |
| 任务恢复 | `POST /api/tasks/{run_id}/resume` | `resume_task(run_id)` | 服务端返回的完整 `TaskView` | 网络/服务错误显示不可用；不创建新 run |
| 模拟账户 | `GET /api/portfolio` | `get_portfolio()` | `cash`, `equity`, `currency`, `positions`, `updated_at`, `valuation_ref` | 失败显示账户不可用；前端不计算、扣减或补充现金/持仓 |
| 公开运行能力 | `GET /api/config/public` | 只读能力声明 | `paper_account`, `supports_live_data`, `capabilities`, `unavailable` | 不含密钥；只展示后端声明，不把未提供能力当成可用 |

## Adapter 行为

`/static/api-adapter.js` 暴露 `window.FinGuardApi` 和 `window.FinGuardApiError`。它只负责 HTTP、JSON、错误归一化、轮询和审批请求去重：

- `createTask`, `getTask`, `getEvents`, `decideProposal`, `resumeTask`, `getPortfolio`, `getPublicConfig` 均直接映射真实路由。
- `decideProposal` 用 `run_id:proposal_id` 去重键复用同一个 in-flight Promise，防止重复点击产生并发审批；每次请求都提交服务端读取的 `expected_version`。
- `startPolling(run_id, ...)` 维护 generation 和 `afterSeq`；切换 run 时停止前一个轮询，过期响应不会污染当前任务。
- HTTP 非 2xx、网络失败、无效 JSON、`VERSION_CONFLICT`、`SERVICE_UNAVAILABLE` 都转为带 `code/status/message` 的 `FinGuardApiError`。
- Adapter 不保存 token/API key，不提供设置、密钥上传、自动授权或模型凭据接口。

## 当前明确不可用（UNAVAILABLE）

以下功能没有 ApplicationService/API 支持，UI 不得伪造控件成功或写入本地状态：

- `LIVE_DATA`：当前 Demo provider 是唯一可运行数据源，公开能力 `supports_live_data=false`。
- `MODEL_CONFIGURATION`：不能通过网页保存模型名、Base URL、API Key 或任何凭据。
- `CONNECTION_CHECK`：没有后端探测模型供应商连接的服务接口。
- `AUTOMATIC_AUTHORIZATION`：没有自动授权/登录/密钥交换接口；审批仍需 `decide_proposal`。
- `RESEARCH_ARCHIVE`、`EVIDENCE_DETAILS`、`TASK_HISTORY`：当前冻结服务没有对应查询 API。

这些缺口需要 Agent A 的后续接口设计；C 不修改 ApplicationService、SQLite Schema 或 FROZEN v1 契约来补齐。

## 安全与来源标记

- 浏览器只接收任务的 `data_mode` 和服务端 DTO；Demo/Live 标识由服务返回，不能由 UI 自己改写成另一种来源。
- 动态状态和事件文本以 text content 或转义后的显示内容写入 DOM；不执行服务端返回的 HTML。
- 账户、持仓、订单和审批状态始终以服务端返回为唯一事实来源。
