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

每个 `tick` 处理**一次**状态转换，然后返回。多个 tick 由外部调度器（消息驱动 / 定时器 / API 调用）连续触发，不在单次 tick 内部循环推进。

```python
async def tick(project_id: str) -> None:
    project = await project_service.get_project(project_id)
    state_machine = StateMachine(project.task_type)

    state = project.current_state
    state_type = state_machine.state_type(state)

    if state_machine.is_terminal(state):
        return

    if state_type == "agent_execution":
        await _execute_agent(project, state_machine)
    elif state_type == "human_confirmation":
        await _handle_human_confirmation(project, state_machine)
    elif state_type == "human_input":
        await _handle_human_input(project, state_machine)
    elif state_type == "system_action":
        await _execute_system_action(project, state_machine)
    elif state_type == "idle":
        return
    # tick 结束，由外部调度决定是否再次进入 tick
```

> 关键约束：单次 `tick` 只执行上述分支中的一个，然后返回。状态转换在分支内部完成；下一次 tick 重新加载 `project.current_state` 决定下一步。这避免了"一次性推进多个状态导致难以调试"，也使 cancel/pause/rollback 等控制命令能在 tick 之间生效。

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

### 8. 决策边界（AI-first / 人工升级）

对于 `human_confirmation` / `human_input` 状态：

1. 生成 `pending_approval` 内容
2. 写入 `STATE.json`
3. 根据 `approval_mode` 决定：
   - 发送飞书卡片
   - 或自动决策
4. **结果汇报卡片要求**：每轮 Agent 执行进入 human_confirmation 状态后，必须先通过 Notification Outbox 发送一张或多张独立的结果汇报卡片，展示本轮输出摘要；然后才发送带操作按钮的确认卡片。汇报卡片和确认卡片必须分开发送，不能合并；审批 payload 不是去重真相源。
5. 默认 `normal` 模式不把确认态等同于人工等待：规划交给 Codex Coordinator，质量 pass 按专业证据自动推进，REWORK 按 blocking feedback 的推荐目标回退。
6. 只有偏好、授权、系统不可观测的外部事实，或 Coordinator 无法形成有证据决策时，才等待用户响应。`strict` 模式仍逐项等待人工。

### 多轮确认与恢复

- 需求、方案、Spec、编码、测试和 Review 都允许多轮；每次重入执行态必须创建新 task_id，并增加该阶段 `round_number`。
- Agent 因信息不足发出 `ask_user`（外部 schema 兼容名，语义为 ClarificationRequest）时，Orchestrator 先交给 Coordinator。Coordinator 仅可依据引用的 active confirmed facts 回答；不足时才进入对应用户输入态。回答写入确认事实并注入下一轮同阶段 Task。
- 用户在确认态选择 `revise` 时，保存本轮产物版本和用户意见，旧 approval 过期，然后回到原执行态。
- 测试或 Review 明确 fail 时直接进入 `REWORK`，不得改写为 complete，也不得由 night/auto 模式自动放行。
- Review 回到 Coding 时，Coder 的输入必须包含完整 reviewer output、用户补充意见和未关闭 blocking issues；下一轮 Reviewer 必须逐项复核 Coder 的 `addressed_feedback`。

### WorkItem v2 与无进展循环

- 每个业务 Task 必须由 WorkItem Compiler 生成 v2 合同，包含 owner、stage kind、显式 AC 范围、feedback routes、完成条件、runtime contract 和性能预算。
- Feedback Router 只读取结构化来源、责任目标和 AC 引用；禁止依据 summary/error message 关键词决定回退。
- runtime/schema/进程失败留在当前 Task 恢复路径；质量证据失败才进入项目 REWORK。
- 同一结构化失败签名累计到第三次时，禁止再次静默采用相同回退目标，必须交给 Coordinator 重新选择策略。
- 若第三次重复后仍需回到同一 owner，Runtime Router 必须优先切换到已安装的另一 headless provider；WorkItem、反馈范围和完成条件保持一致，以减少模型盲区而不要求用户重述问题。
- Coordinator 自动驳回规划产物时，必须在转换回执行态前把 reason/evidence_refs 写成 `coordinator_rejection` FeedbackPacket；Owner 重跑前必须幂等补建旧版本或崩溃窗口中缺失的 packet，禁止只保留 reject 状态边。
- 一次 Acceptance fail 只创建一个主责任反馈；coverage validator 只检查 pass 结论是否有完整证据，不得把 fail 再转写成第二个 specs feedback。
- Acceptance Agent 的缓存键只依赖产品 artifact、workspace 内容、外部证据与执行合同；Tester/Reviewer 的语义输出只影响 Evaluator。任务 ID、profiling、时间戳不参与语义 revision。
- AcceptanceEvidence 按显式 AC 依赖做局部失效；未受影响的 active evidence 可以跨 artifact revision 复用。

### Tick 前后检查点

每次 `tick` 取得项目级锁后按以下顺序处理：

1. 读取数据库中的项目控制状态、运行 Task、唯一 pending approval 和未消费用户输入。
2. 优先处理 cancel、pause、rollback 等控制命令。
3. 若 Agent 正在运行，不启动第二个 Agent；自由文本进入 `pending_user_inputs`。
4. Agent 返回后再次读取控制状态；若状态或轮次已变化，丢弃过期结果。
5. 在 merge/transition 前消费中途输入；影响当前交付物的输入阻止自动确认，并进入人工确认或 REWORK。
6. 提交业务事务后再发送成功反馈卡片；消息发送失败只记录可重试的 delivery 状态，不回滚已提交的业务事实。

### Approval 原子性

- 同一项目同一时刻最多一个 pending approval。
- 响应时必须校验 `approval.status=pending`、`approval.state=project.current_state`、action 属于 options。
- approval 响应、旧 approval 失效和状态转换必须在同一事务中完成。
- 用户响应与系统自动确认竞争时使用 compare-and-set；第一个提交者获胜，后续请求返回幂等结果。

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

- runtime、schema、进程或 merge 失败属于技术失败，在原状态按上限重试
- Tester `passed=false` 或 Reviewer 存在 blocking issue 属于质量失败，生成 feedback packet 并进入 `REWORK`
- 系统展示推荐回退目标，但由用户选择 Requirements / Architecture / Specs / Coding / Testing
- 质量失败不允许 night/auto 模式自动确认通过

### 人工长时间未响应

- 项目保持当前 pending 状态
- 不阻塞其他项目
- 可发送提醒（可选，MVP 不实现）

## 并发控制

- 每个项目同时只能有一个 `tick` 在运行
- 项目级锁必须跨线程、跨 asyncio event loop 生效；HTTP 与飞书 WebSocket 不得各自持有互不相干的 event-loop local lock
- 当前单进程实现使用 `threading.Lock` 的异步适配层；若扩展为多进程/多实例，必须替换为数据库行锁或分布式租约
- running-task 检查、条件状态更新和 stale approval 拒绝作为锁之外的数据库幂等防线
- 全局 Agent 执行受运行时队列限制

## 自适应流程选择

- Coordinator 直接输出 `task_type`；Handler 只校验结构化合同，不扫描用户措辞。
- `feature` 保留 Requirements → Architecture → Specs → Coding → Testing → Review → Acceptance。
- `iteration` 仅用于已绑定 Git 仓库中范围明确、架构不确定性低的局部修改，执行 Coding → Testing → Review → Acceptance。
- `iteration` 创建时必须把已确认 brief 固化为 `ITERATION_BRIEF.json`，为每条 AC 分配稳定 ID，并累积历史项目仍有效的回归 AC。
- 快速路径不得用于完整参考复刻，也不得因为追求速度跳过 Tester、Reviewer 或主动验收。

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

### 9. Reject 交互流程

Reject 分为两种事务：

1. **规划产物驳回**：没有 feedback 时只打开输入 UI，不消费 approval；提交 feedback 后才原子创建 FeedbackPacket、resolve reject、回到 Planner 并启动新轮次。
2. **质量/验收驳回**：feedback 可选；立即原子进入 REWORK，并调用 `ensure_waiting_boundary()` 保证 REWORK 选择卡已经创建后才结束 handler。

禁止使用“approval 已 resolve、状态已回执行态，但等待下一条自由文本才启动”的隐式等待模型。等待用户时必须有 pending approval 或 paused 状态。

`ensure_waiting_boundary(project_id)` 的算法：

```text
读取当前状态
if terminal/idle: return
if human_confirmation/human_input:
    tick exactly once        # create or reuse pending approval/card
    return
tick exactly once            # run current agent/system action
刷新状态
if human_confirmation/human_input:
    tick exactly once        # create or reuse pending approval/card
return
```

human state 的 handler 必须幂等：若相同 project/state 已有 pending approval，直接复用并按需重发同一 approval 的卡片，不能 expire 后生成新 approval ID。

handler 返回前执行 stuck invariant：active 非终态项目必须具有 running Task、pending Approval 或 paused 状态；否则记录结构化 ERROR，并向用户发送可恢复提示。

### 10. 质量确认关

通用交付状态机按 `feature` profile 生成 7 个决策边界（状态类型沿用 `human_confirmation` 以兼容持久化协议，但默认并不都等待人）：

| 节点 | 类型 | reject 目标 |
|---|---|---|
| `REQUIREMENT_CONFIRM_PENDING` | 规划确认 | → `PLANNING_REQUIREMENTS` |
| `ARCHITECTURE_CONFIRM_PENDING` | 规划确认 | → `PLANNING_ARCHITECTURE` |
| `IMPLEMENTATION_SPEC_CONFIRM_PENDING` | 规划确认 | → `PLANNING_IMPLEMENTATION_SPECS` |
| `CODING_CONFIRM_PENDING` | 质量确认关 | → `REWORK` |
| `TESTING_CONFIRM_PENDING` | 质量确认关 | → `REWORK` |
| `REVIEW_CONFIRM_PENDING` | 质量确认关 | → `REWORK` |
| `ACCEPTANCE_PENDING` | 验收确认 | → `REWORK` 或快捷回退 |

质量确认关（CODING/TESTING/REVIEW_CONFIRM_PENDING）的行为：
- 专业 Agent 有效 pass 后自动推进并发送只读汇报卡；用户无需重复批准测试事实
- fail 后进入 `REWORK`；有 blocking feedback 推荐目标时系统自动回退到该阶段
- 仅在推荐目标缺失/冲突，或用户主动选择 `strict` 模式时发送回退选择卡
- 回退到 CODING 时，coder 的输入包含 reviewer output 和未关闭的 blocking issues
- 下一轮 Reviewer 必须逐项复核 coder 的 `addressed_feedback`
- 质量失败不允许 night/auto 模式自动确认通过

### 11. 幂等与并发防护

每次 `_execute_agent` 在启动新 task 前检查该项目在当前状态是否已有 `status == "running"` 的 task：
- 有则跳过，不启动重复 agent
- 防止重复 tick 导致多个 claude 进程操作同一 worktree

已在 `orchestrator.py` 中实现。

仅靠上述 running-task 检查不足以覆盖“检查后、写入前”的竞态。`ProjectLockManager` 必须在 FastAPI 和飞书 WebSocket 所在线程间共享同一把项目锁；审批创建还需以当前状态条件更新为准，状态已离开时拒绝创建旧审批，并在状态转换时过期上一状态的 pending approval。

### 12. Runtime 工具边界与重启恢复

- Orchestrator 将工具白名单和 worktree 传给 Claude Code Runtime，但不执行 Agent 的 Read/Write/Edit/Bash 工具。
- `tool_use` 事件仅用于飞书进度展示；真实 `tool_result` 由 Claude Code CLI 产生。
- 服务启动发现 stale running task 时，先标记该 task 失败，再按项目去重恢复 active agent-execution 项目。
- 恢复通过正常 `tick()` 入口执行，因此必须继续经过项目锁、running-task 幂等检查和 retry 上限判断。
- 恢复失败必须记录结构化 event log；不得让项目静默停在 agent-execution 状态且没有 running task。

### 13. 所有 Agent 的主动提问

- 所有 Agent 均可返回 `ask_user`，不是 Planner 专属能力。
- Orchestrator 持久化 question packet，并进入该 Agent 的 input pending 边界；回答后恢复到 `resume_state`、创建新 round，不得统一回退到 Planner。
- 同一 Agent 可连续多次 `ask_user`。多个 pending clarification 可以共存；用户切换话题不会自动关闭旧问题，只有 parent_id、approval_id、问题 ID 或 Coordinator 明确关联的回答才会 resolve 对应问题。历史问题与回答只追加、不覆盖。
- question packet 同时支持结构化选项和自由文本，包含推荐项、默认假设、blocking、影响范围及 trace IDs。
- blocking question 未解决时拒绝 Agent 的 `complete`；只有用户明确接受默认假设后，非阻塞未决问题才可随产物进入下一质量门。
- `ask_user` 处理优先于阶段自己的 `complete/fail` 质量判断；同一输出不得同时推进业务状态和创建问题。
- 发问 Task 结束为 `waiting_for_user`（或等价可审计状态），回答后创建新 Task/round；不得依赖保持 Claude CLI 进程存活。
- 恢复前重新加载最新项目状态、用户中途输入、产物版本和 open feedback。若版本已变化，问题标记 stale 并重新评估，不直接套用旧回答。
- Conversation Coordinator 是唯一用户澄清入口。它可以用有来源的确认事实回答业务 Agent，但不能创造事实、替代专业产物判断或绕过 owner-only 决策；`project_manager` 仅保留为控制决策辅助能力。

当前实现验收门槛：所有 Agent 输出 schema 均接受统一 `questions` 结构；所有 agent-execution 状态均接受 `ask_user`；ProjectService 具有 question/answer 的持久模型；Feishu/API 共用原子回答入口；上述任一项缺失时，该能力应标为“设计中”，不能标为“已支持”。
