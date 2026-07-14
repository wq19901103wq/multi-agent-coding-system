# Control API 规范

> 版本：v1.1
> 用途：定义 FastAPI 端点的请求/响应格式。

## 基础信息

- 框架：FastAPI
- 启动：`uvicorn mac_system.api.main:app --host 0.0.0.0 --port 8000`
- 前缀：无（根路径）
- 错误格式统一：

```json
{
  "error_code": "INVALID_STATE_TRANSITION",
  "message": "Cannot confirm requirement from state PLANNING_ARCHITECTURE",
  "current_state": "PLANNING_ARCHITECTURE"
}
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

`bound_directory` 可省略。传入时必须是绝对路径；系统会校验 Git 仓库根目录，并把项目元数据写入 `.mac/projects/{project_id}/`。不传时使用系统项目工作区。飞书聊天通过 `/bind-dir` 维护聊天级默认目录，该交互不等同于新增一套 Control API 会话状态。

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

原子响应当前 approval。服务校验 approval 仍为 pending、属于项目当前状态且 action 在 options 中，然后在同一事务内提交 approval 响应和状态转换。重复请求返回第一次处理结果并标记 `already_resolved=true`。

### 会话上下文接口

```text
GET    /conversations/{chat_id}/projects
GET    /conversations/{chat_id}/users/{open_id}/context
PUT    /conversations/{chat_id}/users/{open_id}/context
DELETE /conversations/{chat_id}/users/{open_id}/context
POST   /projects/{project_id}/chats
DELETE /projects/{project_id}/chats/{chat_id}
```

设置 active project 时必须同时验证用户的 ProjectUser 权限和项目的 ProjectChat 可见关系。管理 API 使用受认证主体，不应信任请求方随意填写 open_id。

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

### `POST /webhook/feishu`

接收飞书事件回调。

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
