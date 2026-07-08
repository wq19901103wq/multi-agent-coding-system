# Control API 规范

> 版本：v1.0  
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
  "approval_mode": "normal"
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
  "workspace_path": "/Users/yihanwang/multi-agent-coding-system/projects/proj_20260707_120000_a3f7",
  "created_at": "2026-07-07T12:00:00Z",
  "updated_at": "2026-07-07T12:00:00Z"
}
```

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
