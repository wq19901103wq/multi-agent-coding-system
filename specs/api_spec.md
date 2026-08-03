# Control API 规范

> 版本：v1.2
> 用途：定义 FastAPI 端点的请求/响应格式。
> 状态：本文件存在设计意图与实现偏差。已确认的偏差在对应小节以 **[实现偏差]** 标注；未标注处以代码为权威。

## 基础信息

- 框架：FastAPI
- 启动：`uvicorn mac_system.api.main:app --host 0.0.0.0 --port 18080`（端口由 `MCS_PORT` 环境变量配置，默认 18080，见 `.env`、`scripts/start_server.sh:21`）
- 前缀：根路径无前缀；管理面有 `/admin` 前缀（见下文）
- 错误格式：**[实现偏差]** 实际使用 FastAPI 默认错误响应 `{"detail": "<message or code>"}`，本文件早期版本描述的 `{error_code, message, current_state}` 自定义信封未实现。错误码以字符串形式放在 `detail` 字段中。

```json
{"detail": "PROJECT_NOT_FOUND"}
```

## 端点

### `POST /projects`

创建新项目。

**Request：**

```json
{
  "name": "config-validator-cli",
  "task_type": "feature",
  "description": "实现一个 config-validator CLI 工具...",
  "approval_mode": "normal",
  "bound_directory": "/absolute/path/to/existing-repository"
}
```

`task_type` 支持 `feature`、`iteration`、`bug_fix`、`documentation`。其中 `iteration` 只用于已有绑定仓库，并要求请求携带非空 `confirmed_brief.scope` 与 `confirmed_brief.acceptance_criteria`；普通客户端不应把它用于创建全新项目。

**Response：**

```json
{
  "id": "proj_20260707_120000_a3f7",
  "name": "config-validator-cli",
  "task_type": "feature",
  "description": "实现一个 config-validator CLI 工具...",
  "status": "active",
  "current_state": "PLANNING_REQUIREMENTS",
  "previous_state": null,
  "workspace_path": "/absolute/path/to/existing-repository",
  "bound_directory": "/absolute/path/to/existing-repository",
  "created_at": "2026-07-07T12:00:00Z",
  "updated_at": "2026-07-07T12:00:00Z"
}
```

`bound_directory` 可省略。传入时必须是绝对路径；系统会校验 Git 仓库根目录，并把项目元数据写入 `.mac/projects/{project_id}/`。不传时使用系统项目工作区。飞书聊天通过 `/bind` 维护聊天级默认目录，该交互不等同于新增一套 Control API 会话状态。

### `GET /projects/{project_id}`

查询项目详情。

**Response：** 同 `POST /projects` 响应。

### `GET /projects/{project_id}/status`

查询项目状态摘要。

**Response：**

```json
{
  "project_id": "proj_20260707_120000_a3f7",
  "current_state": "REQUIREMENT_CONFIRM_PENDING",
  "display": "等待确认需求",
  "status": "active",
  "pending_approval": {
    "approval_id": "appr_20260707_120100_b2c9",
    "type": "confirmation",
    "question": "请确认以下需求理解",
    "options": [...]
  },
  "recent_events": [
    {"level": "INFO", "message": "Project created", "created_at": "..."}
  ]
}
```

### `POST /projects/{project_id}/decisions`

提交人工决策。

**Request：**

```json
{
  "approval_id": "appr_20260707_120100_b2c9",
  "action": "confirm",
  "message": "确认，范围校验也做"
}
```

`action` 值来自当前 `pending_approval.options[].action`。

**Response：**

```json
{
  "project_id": "proj_20260707_120000_a3f7",
  "previous_state": "REQUIREMENT_CONFIRM_PENDING",
  "current_state": "PLANNING_ARCHITECTURE",
  "updated_at": "2026-07-07T12:05:00Z"
}
```

### `POST /projects/{project_id}/messages`

用户追问或发送非命令消息。

消息处理取决于项目实时状态：

- `*_INPUT_PENDING`：作为当前唯一 input approval 的回答。
- `*_CONFIRM_PENDING`：保存为当前产物评论；只有明确 action 才改变状态。
- Agent 正在运行：返回 `202 Accepted`，将消息写入持久化 pending_user_inputs，稍后在 merge/transition 前消费。
- 消息包含范围变更或纠正：生成 feedback packet 并阻止该轮自动确认。
- 普通问题：可由 project_manager 回答，但不得静默修改已确认产物。

飞书自由文本进入本端点前必须已由 conversation routing 解析出唯一 project_id。无 active project 或存在歧义时返回项目选择响应，不得按用户最近项目猜测。

**Request：**

```json
{
  "content": "数据库为什么选 SQLite？"
}
```

**Response：**

```json
{
  "project_id": "proj_20260707_120000_a3f7",
  "reply": "SQLite 零配置，适合 MVP 阶段验证。",
  "state": "REQUIREMENT_CONFIRM_PENDING"
}
```

排队响应示例：

```json
{
  "project_id": "proj_...",
  "input_id": "input_...",
  "status": "queued",
  "observed_state": "CODING",
  "message": "输入已保存，将在本轮 Agent 结束后优先处理"
}
```

### `POST /projects/{project_id}/approvals/{approval_id}/resolve`

**[实现偏差]** 此端点为设计意图，当前未单独实现。实际响应 approval 的入口是 `POST /projects/{project_id}/decisions`（携带 `approval_id` 与 `action`）。`/decisions` 端点已涵盖 approval 响应、状态转换与幂等校验。

### 会话上下文接口

**[实现偏差]** 以下端点为设计意图，当前未实现：

```text
GET    /conversations/{chat_id}/projects
GET    /conversations/{chat_id}/users/{open_id}/context
PUT    /conversations/{chat_id}/users/{open_id}/context
DELETE /conversations/{chat_id}/users/{open_id}/context
POST   /projects/{project_id}/chats
DELETE /projects/{project_id}/chats/{chat_id}
```

会话与项目的绑定目前通过飞书 `/bind` 文本命令和 admin HTML 面板维护，无 REST 端点。

设置 active project 时必须同时验证用户的 ProjectUser 权限和项目的 ProjectChat 可见关系。管理 API 使用受认证主体，不应信任请求方随意填写 open_id。

### 管理面接口（`/admin` 前缀）

实际实现的管理面端点（HTML 与 action 端点），认证依赖 `_admin_token_dependency`：

```text
GET    /admin/projects                       管理面项目列表（HTML）
GET    /admin/projects/{project_id}          管理面项目详情（HTML）
POST   /admin/projects/{project_id}/cancel   取消项目
POST   /admin/projects/{project_id}/tick     手动触发一次 tick
POST   /admin/projects/{project_id}/rollback 回退到指定状态
```

> **安全提示**：当前 `_admin_token_dependency` 通过 query string 传递 token，且在 `admin_token` 未配置时 fail-open。详见代码 `src/mac_system/api/admin_routes.py:24-27`。

### `POST /projects/{project_id}/commands`

用户命令（`/pause`、`/resume`、`/cancel`、`/rollback`、`/status`）。

**Request：**

```json
{
  "command": "pause"
}
```

或 `/rollback`：

```json
{
  "command": "rollback",
  "target_state": "PLANNING_REQUIREMENTS"
}
```

**Response：**

```json
{
  "project_id": "proj_20260707_120000_a3f7",
  "command": "pause",
  "previous_state": "CODING",
  "current_state": "PAUSED",
  "message": "项目已暂停"
}
```

### `POST /events`

**[实现偏差]** 飞书事件回调实际入口为 `/events`（由 `feishu_routes.py:56` 定义），不是 `/webhook/feishu`。当 `feishu_transport=websocket` 时此端点不接收事件，仅作为 HTTP 兜底。

**Request：** 飞书原始事件 payload。

**Response：**

```json
{
  "code": 0,
  "message": "ok"
}
```

处理流程：
1. 校验飞书 token/encrypt
2. 解析 `open_id`
3. 查询绑定的 `project_id`
4. 判断是卡片按钮点击还是文本消息
5. 路由到对应处理逻辑

### `GET /health`

健康检查。

**Response：**

```json
{"status": "ok"}
```

## 错误码

| 错误码 | 含义 |
|--------|------|
| `PROJECT_NOT_FOUND` | 项目不存在 |
| `INVALID_STATE_TRANSITION` | 非法状态转换 |
| `APPROVAL_NOT_FOUND` | 审批记录不存在 |
| `APPROVAL_ALREADY_RESPONDED` | 审批已响应 |
| `INVALID_COMMAND` | 非法命令 |
| `TARGET_STATE_UNREACHABLE` | 回退目标状态不可达 |
| `FEISHU_VERIFICATION_FAILED` | 飞书回调校验失败 |
