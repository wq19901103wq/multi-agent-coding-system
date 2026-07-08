# AgentOrchestrator 规范

> 版本：v1.0  
> 用途：定义编排器的核心流程，它是连接状态机、ProjectService、AgentRuntime、WorktreeManager、FeishuBridge 的协调中心。

## 职责

1. 监听项目状态变化
2. 根据当前状态决定调用哪个 Agent
3. 准备 worktree 和 `.mac/` 上下文
4. 调用 `AgentRuntime.execute()`
5. 解析 Agent 输出，推进状态机
6. 在需要人工确认时通知 `FeishuBridge`
7. 处理失败、重试、回滚

## 主循环

```python
async def tick(project_id: str) -> None:
    project = await project_service.get_project(project_id)
    state_machine = StateMachine(project.task_type)

    while True:
        state = project.current_state
        state_type = state_machine.state_type(state)

        if state_machine.is_terminal(state):
            break

        if state_type == "agent_execution":
            await _execute_agent(project, state_machine)
        elif state_type == "human_confirmation":
            await _handle_human_confirmation(project, state_machine)
        elif state_type == "human_input":
            await _handle_human_input(project, state_machine)
        elif state_type == "system_action":
            await _execute_system_action(project, state_machine)
        elif state_type == "idle":
            break

        project = await project_service.get_project(project_id)
```

> 注意：每个 `tick` 只处理一个状态转换，然后重新加载项目状态。避免一次性推进多个状态导致难以调试。

## Agent 执行流程

### 1. 确定 Agent 和 Stage

```python
agent_type = state_machine.agent_for(state)
stage = state_machine.stage_for(state)
```

### 2. 创建 Task

```python
task = await project_service.create_task(
    project_id=project.id,
    agent_type=agent_type,
    state=state,
    input_data={...},
)
```

### 3. 创建 Worktree

```python
worktree = await worktree_manager.create_worktree(
    project_id=project.id,
    task_id=task.id,
    agent_type=agent_type,
    stage=stage,
)
```

### 4. 准备 `.mac/` 上下文

写入 worktree 的 `.mac/`：

- `STATE.json`：当前状态、pending_approval、context
- `REQUIREMENTS.json`（若存在）
- `ARCHITECTURE.json`（若存在）
- `SPECS.json`（若存在）
- `BUG_ANALYSIS.json` / `DOC_OUTLINE.json`（对应任务类型）
- `TASK.json`：input 部分

### 5. 渲染 Prompt

```python
config = agent_config[agent_type]
system_prompt = render_system_prompt(config, stage)
task_prompt = render_task_prompt(config, project, state, worktree)
output_schema = Path(config[stage or "default"]["output_schema"])
allowed_tools = config.get("default_allowed_tools", ["Read", "Edit", "Write", "Bash"])
```

### 6. 调用 AgentRuntime

```python
result = await agent_runtime.execute(
    agent_type=agent_type,
    task_prompt=task_prompt,
    system_prompt=system_prompt,
    workspace=project.workspace_path,
    worktree=worktree,
    context_files=context_files,
    output_schema=output_schema,
    allowed_tools=allowed_tools,
    readonly_dirs=[project.workspace_path],
    timeout_seconds=settings.agent_timeout_seconds,
)
```

### 7. 处理结果

#### 成功

1. 校验 `result.output_json` 符合 schema
2. 更新 `TASK.json` 的 output
3. 根据 Agent 类型处理产物：
   - planner：将输出写入 `.mac/REQUIREMENTS.json` 等
   - coder/tester/reviewer：将 worktree 变更合并到主工作区
4. 调用 `project_service.complete_task(task.id, result.output_json, result.cost_info)`
5. 调用 `project_service.transition_state(project.id, result.output_json["next_state_event"])`

#### 失败

1. 记录失败原因
2. 调用 `project_service.fail_task(task.id, error_message)`
3. 若 `retry_count < MAX_AGENT_RETRIES`：
   - 清理 worktree
   - 重新创建 task 和 worktree
   - 重试
4. 若超过最大重试：
   - 进入 `FAILED` 状态

### 8. 人工确认状态

对于 `human_confirmation` / `human_input` 状态：

1. 生成 `pending_approval` 内容
2. 写入 `STATE.json`
3. 根据 `approval_mode` 决定：
   - 发送飞书卡片
   - 或自动决策
4. 等待用户响应（异步，由 Feishu 回调或 API 触发下一次 `tick`）

## 系统动作执行

### `INIT`

- 已由 `ProjectService.create_project` 完成
- `tick` 时自动触发 `start` 事件

### `MERGING`

1. 在主工作区执行 `git add -A`
2. 生成 commit message
3. `git commit -m "{message}"`
4. 若配置 remote：`git push origin feature/{project_id}`
5. 成功：事件 `complete` → `COMPLETED`
6. 失败：事件 `fail` → `FAILED`

## Commit Message 生成

默认格式：

```
[{agent_type}] {summary}

- Task: {task_id}
- State: {state}
- Modified files:
  - {file1}
  - {file2}
```

也可由 Agent 在 `TASK.json` 的 `output.commit_message` 中提供，Orchestrator 优先使用。

## 失败恢复

### Agent 执行失败

- 重试同状态最多 `MAX_AGENT_RETRIES` 次
- 每次重试使用新 task_id 和新 worktree
- 保留最近失败的 worktree 7 天

### 测试/Review 失败

- 重试最多 `MAX_TEST_RETRIES` / `MAX_REVIEW_RETRIES` 次
- 每次回到 `CODING` 状态
- 超过后进入 `FAILED`

### 人工长时间未响应

- 项目保持当前 pending 状态
- 不阻塞其他项目
- 可发送提醒（可选，MVP 不实现）

## 并发控制

- 每个项目同时只能有一个 `tick` 在运行
- 使用项目级锁（如数据库行锁或 asyncio Lock）
- 全局 Agent 执行受运行时队列限制

## 事件日志

每次 `tick` 记录：

```json
{
  "level": "INFO",
  "message": "State transition: PLANNING_REQUIREMENTS -> REQUIREMENT_CONFIRM_PENDING",
  "meta": {
    "project_id": "proj_...",
    "from_state": "PLANNING_REQUIREMENTS",
    "to_state": "REQUIREMENT_CONFIRM_PENDING",
    "event": "complete"
  }
}
```
