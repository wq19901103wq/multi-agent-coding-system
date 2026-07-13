# 并发控制与用户命令规范

> 版本：v1.0  
> 用途：定义多项目并发执行规则，以及用户 `/pause`、`/resume`、`/cancel`、`/rollback`、`/status` 命令的行为。

## 全局运行时队列

### 设计目标

- 防止本地机器因同时运行多个 `claude -p` 实例而卡死
- 保证同一项目内的 Agent 顺序执行
- 不同项目之间可以并发（受队列容量限制）

### 队列配置

```python
MAX_CONCURRENT_AGENT_EXECUTIONS = 2  # 默认同时最多 2 个 Agent
```

### 调度规则

1. 每个项目有一个独立的 **项目队列**，存放等待执行的 Agent Task。
2. 全局有一个 **执行槽位池**，容量为 `MAX_CONCURRENT_AGENT_EXECUTIONS`。
3. 当项目队列中有 Task 且全局有空闲槽位时，取出该 Task 执行。
4. 同一项目的 Task 不能同时占用两个槽位（避免同一项目并发修改状态）。
5. 等待中的项目状态保持为当前状态（如 `PLANNING_REQUIREMENTS`），`Task.status = pending`。

### 状态同步

- Task 开始执行：`Task.status = running`
- Task 执行完成：`Task.status = completed` / `failed`
- 项目状态变更：由 Orchestrator 在 Task 完成后根据 `output.next_state_event` 更新

## 用户命令

用户可通过飞书或 API 发送命令。命令以 `/` 开头。

### `/use {project_id}` 与 `/current`

- `/use` 更新 `(chat_id, open_id)` 的 ChatUserContext，不改变 Project.current_state/status，不中断正在运行的 Agent。
- `/current` 只读返回当前上下文。
- 卡片 approval_id 和显式 project_id 不受当前上下文切换影响。
- pending user input 在创建时已固化 project_id，切换后不得迁移到新项目。
- 群聊中上下文按用户隔离；不同成员切换项目互不影响。

### `/pause {project_id}`

- **触发**：用户要求暂停项目
- **行为**：
  1. 若当前项目有正在运行的 Agent：
     - 将 `Project.status` 设为 `PAUSED`
     - 保存 `current_state` 到 `previous_state`
     - **不强制终止**当前 Agent（避免产物损坏）
     - 当前 Agent 执行完成后，Orchestrator 检查到 `Project.status == PAUSED`，丢弃该 Agent 输出（不推进状态）
  2. 若当前项目没有运行中的 Agent：
     - 立即进入 `PAUSED` 状态
- **响应**：发送暂停确认消息

### `/resume {project_id}`

- **触发**：用户要求恢复项目
- **行为**：
  1. 将 `Project.status` 从 `PAUSED` 恢复为 `ACTIVE`
  2. 读取 `previous_state`
  3. 若 `previous_state` 是 `agent_execution` 类型，创建新 Task 重新执行
  4. 若 `previous_state` 是 `human_confirmation` / `human_input` 类型，重新发送卡片/消息
- **响应**：发送恢复确认消息

### `/cancel {project_id}`

- **触发**：用户要求取消项目
- **行为**：
  1. 将 `Project.status` 设为 `CANCELLED`
  2. 若当前有运行中的 Agent：
     - 标记该 Task 为 `cancelled`
     - 不强制终止 Agent，但丢弃其输出
  3. 清理该项目所有 pending Task
  4. 终端状态，不再推进
- **响应**：发送取消确认消息

### `/rollback {project_id} [to_state]`

- **触发**：用户要求回退到某个状态
- **参数**：
  - `to_state`：可选，目标状态名，如 `PLANNING_REQUIREMENTS`、`CODING`
  - 若省略，系统提示用户选择
- **行为**：
  1. 验证 `to_state` 是否在当前状态机中可到达
  2. 将 `Project.current_state` 设为 `to_state`
  3. 若当前有运行中的 Agent：
     - 标记该 Task 输出将被丢弃
  4. 清理与目标状态不一致的 pending Task
  5. 恢复执行时从 `to_state` 开始
- **响应**：发送回退确认消息

### `/status {project_id}`

- **触发**：用户查询项目状态
- **行为**：
  1. 查询 `Project` 当前状态
  2. 查询最近 5 条 `event_logs`
  3. 生成状态摘要
- **响应**：发送状态卡片/文本消息

## Agent 执行期间的命令竞态

### 竞态处理原则

1. 用户命令**只修改项目目标状态**，不直接修改当前运行中 Agent 的执行环境。
2. Agent 执行完成后，Orchestrator **先检查 `Project` 状态** 是否被命令修改：
   - 若已被修改（如 `PAUSED`、`CANCELLED`、回退到某状态）：
     - 丢弃 Agent 输出
     - 按最新项目状态继续
   - 若未被修改：
     - 正常解析 Agent 输出并推进状态

### 时序示例

```
t0: Project 在 CODING 状态，Coder Agent 开始执行
t1: 用户发送 /pause proj_001
t2: Project.status 变为 PAUSED，previous_state = CODING
t3: Coder Agent 执行完成，输出 complete
t4: Orchestrator 检查到 Project.status == PAUSED
t5: Orchestrator 丢弃 Coder 输出，发送「已暂停」消息
```

## 中途自由文本队列

Agent 运行期间收到的非命令文本不得直接注入正在运行的进程，也不得当作普通闲聊丢弃。系统写入持久化 `pending_user_inputs`：

```json
{
  "input_id": "input_...",
  "project_id": "proj_...",
  "sender_id": "ou_...",
  "observed_state": "CODING",
  "content": "补充：Windows 路径也必须支持",
  "kind": "scope_change",
  "status": "pending",
  "created_at": "...",
  "consumed_by_task_id": null
}
```

消费规则：

1. 控制命令优先于自由文本；`cancel > rollback > pause > approval response > feedback > question`。
2. Agent 完成后、merge 和 transition 前重新查询队列。
3. `scope_change` / `correction` 阻止自动确认并进入 `REWORK`；`question` 可由 project_manager 回答，但不得改变已确认范围。
4. 多条输入按 `created_at, input_id` 顺序处理，成功处理后记录消费 Task，不物理删除。
5. 服务重启后从数据库继续消费，不能依赖内存队列。

## Approval 与自动确认竞态

- 每项目最多一个 pending approval；重复 tick 复用该 approval，不重复发卡。
- 自动确认只适用于 Agent 已明确 pass 的质量门。
- 自动确认与用户响应通过条件更新 `WHERE status='pending'` 竞争；更新行数为 0 的一方读取最终结果并返回幂等响应。
- pause、rollback 或新 scope-change feedback 到达后，应取消该轮自动确认资格，即使自动任务已排队。

## 飞书消息命令解析

飞书文本消息按以下规则解析：

1. 若以 `/` 开头，按命令处理。
2. 若不是命令且当前项目处于 `*_CONFIRM_PENDING` 或 `CLARIFICATION_PENDING`：
   - 视为追问或澄清回答
   - 调用 `project_manager` Agent 解析意图
3. 若不是命令且无 pending 审批：
   - 视为普通追问
   - 由 `project_manager` Agent 回答

## 异常命令

- 命令格式错误：回复帮助信息
- `project_id` 不存在：回复错误
- 用户无权限操作该项目：回复错误
- 目标状态不可达：回复可用状态列表
