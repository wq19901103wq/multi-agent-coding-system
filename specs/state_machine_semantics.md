# 状态机事件语义

> 版本：v1.0  
> 用途：补充 `state_machines/*.json` 中事件名的具体含义、触发条件、副作用和边界行为。

## 通用约定

1. **状态机是项目级**：一个项目在同一时刻只有一个 `current_state`。
2. **事件由三类来源产生**：
   - **Agent 输出**：Agent 执行完成后在 `TASK.json` 的 `output.next_state_event` 中声明。
   - **人工决策**：用户通过飞书卡片或 API 提交决策。
   - **系统动作**：Orchestrator 自动完成（如 `INIT.start`、`MERGING.complete`）。
3. **非法事件**：若当前状态不支持该事件，Orchestrator 返回 `INVALID_STATE_TRANSITION` 错误，状态不变。
4. **状态进入/退出副作用**：每次进入新状态时，Orchestrator 会：
   - 更新 `Project.current_state` 和 `Project.previous_state`
   - 在 `STATE.json` 的 `history` 中追加记录
   - 根据状态类型触发对应动作（Agent 执行 / 发送飞书卡片 / 系统动作）

## 状态类型总览

| 类型 | 行为 | 典型状态 |
|------|------|---------|
| `system_action` | Orchestrator 自动执行，成功后发出事件 | `INIT`, `MERGING` |
| `agent_execution` | 调用 AgentRuntime 执行，完成后解析 `TASK.json` | `PLANNING_*`, `CODING`, `TESTING`, `REVIEW` |
| `human_confirmation` | 发送确认卡片，等待用户二选一/多选一 | `*_CONFIRM_PENDING` |
| `human_input` | 发送输入卡片，等待用户文本回复 | `CLARIFICATION_PENDING`, `CONFLICT_PENDING` |
| `terminal` | 流程结束，不再自动推进 | `COMPLETED`, `FAILED`, `CANCELLED` |
| `idle` | 流程暂停，等待 `/resume` 命令 | `PAUSED` |

## 按状态的事件语义

### `INIT` → 任务类型对应的初始状态

- **事件**：`start`
- **触发**：创建项目后 Orchestrator 自动触发。
- **目标状态**：
  - `feature` 任务 → `PLANNING_REQUIREMENTS`
  - `bug_fix` 任务 → `BUG_ANALYSIS`
  - `documentation` 任务 → `DOC_OUTLINE`
- **副作用**：
  - 初始化 `projects/{project_id}/` 目录
  - 执行 `git init`
  - 写入初始 `STATE.json`、`TASK.json`
  - 创建第一条 `Task` 记录（planner / requirements）

### `PLANNING_REQUIREMENTS` → `REQUIREMENT_CONFIRM_PENDING`

- **事件**：`complete`
- **触发**：planner stage=requirements 成功输出 `REQUIREMENTS.json`。
- **副作用**：
  - 将 `REQUIREMENTS.json` 合并到主工作区
  - 生成 `REQUIREMENT_CONFIRM_PENDING` 审批记录
  - 根据 `approval_mode` 决定：
    - 若当前状态在 `require_confirmation_for` 列表中：发送飞书确认卡片
    - 若当前状态不在列表中（如 `auto` 模式的需求确认）：
      - 直接视为 `confirm` 并推进
      - 记录一条自动决策日志到 `approvals` 表，状态为 `auto_resolved`

### `REQUIREMENT_CONFIRM_PENDING`

- **`confirm`**：进入 `PLANNING_ARCHITECTURE`
- **`reject`**：回到 `PLANNING_REQUIREMENTS`，保留用户评论作为 planner 输入

### `PLANNING_ARCHITECTURE` → `ARCHITECTURE_CONFIRM_PENDING`

- **事件**：`complete`
- **触发**：planner stage=architecture 成功输出 `ARCHITECTURE.json`。
- **副作用**：
  - 合并 `ARCHITECTURE.json`
  - 将 `decisions` 渲染为确认卡片按钮

### `ARCHITECTURE_CONFIRM_PENDING`

- **`confirm`**：进入 `PLANNING_SPECS`
- **`reject`**：回到 `PLANNING_ARCHITECTURE`
- **`select_option`**：
  - 用户选择了 Architect 给出的某个决策选项
  - 请求体中携带 `decision_id` 和 `option_key`
  - Orchestrator 将选择结果追加到 `ARCHITECTURE.json` 的 `resolved_decisions` 中
  - 重新进入 `PLANNING_ARCHITECTURE` 让 planner 按选择重写方案

> 说明：`select_option` 事件的结构为：事件名 `select_option`，payload 中 `{decision_id, option_key}`。状态机 JSON 中统一用 `select_option` 作为事件名。

### `PLANNING_SPECS` → `SPEC_CONFIRM_PENDING`

- **事件**：`complete`
- **触发**：planner stage=specs 成功输出 `SPECS.json`。
- **副作用**：合并 `SPECS.json`。

### `SPEC_CONFIRM_PENDING`

- **`confirm`**：进入 `CODING`
- **`reject`**：回到 `PLANNING_SPECS`

### `CODING`

Agent：`coder`

| 事件 | 含义 | 触发条件 |
|------|------|---------|
| `complete` | 编码完成 | Coder 输出 `success=true`，所有 Spec 已实现 |
| `fail` | 编码失败 | Coder 输出 `success=false`，无法继续 |
| `clarify` | 需要澄清 | Coder 在 `output.clarification_question` 中提出问题 |
| `conflict` | 设计冲突 | Coder 认为 Spec/方案存在冲突，在 `output.conflict` 中描述 |
| `new_requirement` | 发现新需求 | Coder 认为需要回到需求层 |
| `architecture_question` | 方案疑问 | Coder 认为需要回到架构层 |
| `spec_question` | Spec 疑问 | Coder 认为需要回到 Spec 层 |

> 后四个事件（`new_requirement` / `architecture_question` / `spec_question` / `conflict`）本质都是「请求回退」。Coder 在 `TASK.json` 的 `output.request_rollback_to` 字段写明目标状态，Orchestrator 映射为对应事件。
>
> 对于 `bug_fix` 任务，`CODING` 还支持 `bug_analysis_question` 回退到 `BUG_ANALYSIS`。
>
> 对于 `documentation` 任务，`CODING` 还支持 `outline_question` 回退到 `DOC_OUTLINE`。

### `CLARIFICATION_PENDING`

- **`answer`**：用户回答了问题，回到 `CODING`，回答内容写入 `STATE.json` 的 `context.clarification_answer`
- **`needs_spec_change`**：用户回答导致需要改 Spec，回到 `PLANNING_SPECS`
- **`needs_architecture_change`**：用户回答导致需要改方案，回到 `PLANNING_ARCHITECTURE`
- **`needs_bug_analysis_change`**（bug_fix 任务）：回到 `BUG_ANALYSIS`
- **`needs_outline_change`**（documentation 任务）：回到 `DOC_OUTLINE`

### `CONFLICT_PENDING`

- **`resolve`**：用户裁决后继续，回到 `CODING`，裁决内容写入 `context.conflict_resolution`
- **`needs_spec_change`**：回到 `PLANNING_SPECS`
- **`needs_architecture_change`**：回到 `PLANNING_ARCHITECTURE`
- **`needs_bug_analysis_change`**（bug_fix 任务）：回到 `BUG_ANALYSIS`
- **`needs_outline_change`**（documentation 任务）：回到 `DOC_OUTLINE`

### `TESTING`

Agent：`tester`

| 事件 | 含义 | 处理 |
|------|------|------|
| `complete` | 测试通过 | 进入 `REVIEW` |
| `fail` | 测试失败 | `retry_count += 1`；若 `retry_count <= MAX_TEST_RETRIES` 回到 `CODING`，否则事件 `fail_max_retries` |
| `fail_max_retries` | 测试失败超过最大重试 | 进入 `FAILED` |

### `REVIEW`

Agent：`reviewer`

| 事件 | 含义 | 处理 |
|------|------|------|
| `complete` | Review 通过 | 进入 `ACCEPTANCE_PENDING` |
| `fail` | Review 不通过 | `retry_count += 1`；若 `retry_count <= MAX_REVIEW_RETRIES` 回到 `CODING`，否则事件 `fail_max_retries` |
| `fail_max_retries` | Review 不通过超过最大重试 | 进入 `FAILED` |

### `ACCEPTANCE_PENDING`

- **`confirm`**：进入 `MERGING`
- **`reject`**：回到 `CODING`（默认实现层问题）

按任务类型提供的回退选项：

- **`feature`**：
  - `reject_to_requirements`：回到 `PLANNING_REQUIREMENTS`
  - `reject_to_architecture`：回到 `PLANNING_ARCHITECTURE`
  - `reject_to_specs`：回到 `PLANNING_SPECS`
- **`bug_fix`**：
  - `reject_to_analysis`：回到 `BUG_ANALYSIS`
- **`documentation`**：
  - `reject_to_outline`：回到 `DOC_OUTLINE`

> 用户驳回时，系统通过 `acceptance_evaluator` 或 `project_manager` Agent 生成根因分析，推荐回退阶段，但最终由用户选择。

### `MERGING`

- **事件**：`complete`
- **系统动作**：
  1. 将所有已合并到主工作区的变更 `git add`
  2. 生成 commit message（由 Agent 或模板生成）
  3. `git commit`
  4. 若配置了 `GIT_REMOTE_URL`，`git push` 到 `feature/{project_id}` 分支
- **失败**：事件 `fail` → `FAILED`

### `FAILED` / `COMPLETED` / `CANCELLED`

- 终端状态，不再自动推进。
- 进入 `FAILED` 时，Orchestrator 发送通知消息到飞书，附带最后一条错误日志。

## 重试计数器规则

1. **计数器存储位置**：`STATE.json` 的 `context.retry_counts.{STATE_NAME}`，以及数据库 `Task.retry_count`。
2. **何时重置**：当状态离开 `TESTING` 或 `REVIEW` 进入非失败目标状态时，对应计数器重置为 0。
3. **最大重试次数**：
   - 测试失败：`MAX_TEST_RETRIES`（默认 2）
   - Review 失败：`MAX_REVIEW_RETRIES`（默认 2）
   - Agent 执行失败（JSON 解析失败等）：`MAX_AGENT_RETRIES`（默认 3）
4. **Agent 执行失败重试**：若 `PLANNING_*` / `CODING` 等 Agent 执行返回 `success=false`，Orchestrator 会先重试同一次 Task（不创建新 worktree，或清理后重建），最多 `MAX_AGENT_RETRIES` 次，之后进入 `FAILED`。

## 暂停 / 恢复规则

1. **触发暂停**：用户通过 API 或飞书发送 `/pause` 命令，或系统检测到需要等待长时间外部响应时。
2. **状态转换**：从当前状态 `S` 进入 `PAUSED`，并将 `S` 保存到 `Project.previous_state` 和 `STATE.json` 的 `previous_state`。
3. **恢复**：用户发送 `/resume` 后，Orchestrator 读取 `previous_state` 并返回该状态；若该状态是 `agent_execution`，则重新触发一次 Agent 执行。
4. **pending 审批期间暂停**：若用户在 `*_CONFIRM_PENDING` 状态下发送 `/pause`，恢复后仍停留在原 pending 状态，继续等待用户决策。

## 回滚规则

1. **状态回滚**：状态机事件直接驱动，如 `reject` 回到上一阶段。
2. **worktree 回滚**：回退到 `CODING` 之前的阶段时，当前 Coder worktree 保留 24 小时供查看，之后由清理任务删除；主工作区保持不变（因为尚未合并）。
3. **已合并产物回滚**：若 `ACCEPTANCE_PENDING` 驳回并要求回退到 `PLANNING_*`，已合并到主工作区的代码保留在 feature 分支历史中，不自动 revert；新 worktree 基于当前主工作区创建。

## 自动决策规则

当状态进入 `human_confirmation` 或 `human_input` 类型时：

1. 查询当前 `approval_mode` 配置。
2. 若当前状态需要确认，则发送飞书卡片，等待用户决策。状态需要确认的条件是：
   - 状态名在 `require_confirmation_for` 列表中；或
   - 状态类型在 `require_confirmation_for_types` 列表中（如 `human_confirmation`、`human_input`）。
3. 若当前状态不需要确认：
   - `human_confirmation`：直接触发 `confirm` 事件，记录 `auto_resolved` 决策日志。
   - `human_input`（如 `CLARIFICATION_PENDING`）：
     - 调用 `project_manager` Agent 基于上下文生成默认回答
     - 以 `answer` 事件推进
     - 记录 `auto_resolved` 决策日志

### `auto_resolve_strategy`

- `none`：不自动决策，必须人工确认。
- `project_manager_default`：由 `project_manager` Agent 生成默认回答/选择。

## 夜间模式

- 夜间窗口由 `approval_modes.json` 中 `night_window` 定义，默认 `23:00 - 08:00`，时区 `Asia/Shanghai`。
- 进入夜间窗口时，新项目若使用 `night` 模式则按夜间规则执行；已运行项目不自动切换模式。
- 跨窗口的 pending 审批保持原有模式规则，不中途变更。

## 异常事件

| 事件 | 含义 | 处理 |
|------|------|------|
| `timeout` | Agent 执行超时 | 视为失败，计入重试；超时时长由 `AGENT_TIMEOUT_SECONDS` 控制 |
| `json_parse_error` | Agent 输出 JSON 无法解析 | 视为失败，计入 `MAX_AGENT_RETRIES` |
| `schema_validation_error` | Agent 输出 JSON 不符合 schema | 视为失败，计入 `MAX_AGENT_RETRIES` |
| `merge_conflict` | Reviewer 合并 worktree 遇到无法自动解决的 git 冲突 | 进入 `CONFLICT_PENDING` |
