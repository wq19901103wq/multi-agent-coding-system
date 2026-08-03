# 状态机事件语义

> 版本：v2.0
> 用途：补充通用 `state_machines/delivery.json` 与 `workflow_profiles/*.json` 的事件含义、触发条件、副作用和边界行为。

## 配置模型：一个状态机，多个流程 Profile

系统只有一个交付生命周期状态机。`delivery.json` 定义 INIT、阶段执行、等待输入、人工确认、REWORK、验收、合并和终态的统一事件语义；`workflow_profiles/{task_type}.json` 只声明该任务类型有哪些阶段、由哪个 Agent 执行、产物确认名以及允许回退到哪些阶段。

每个 Agent 执行态由编译器生成对应的 `<EXECUTION_STATE>_RECOVERY_PENDING`。网络、模型、Schema、进程或合并等技术失败会在同一 tick 内按上限自动重试；耗尽后触发 `runtime_exhausted` 进入恢复态，由用户选择 `retry`、适用时 `choose_rework` 或 `cancel`。技术失败不得伪装为质量 `fail`，也不得未经用户选择直接终止项目。

Profile 不得复制完整 `states` 或自行重定义 `complete / fail / ask_user / confirm / reject` 语义。新增 `refactor`、`migration` 等任务类型时，应优先新增 profile；只有所有任务都需要改变的生命周期规则才允许修改 `delivery.json`。

运行时将模板和 profile 编译为现有的具体状态名，因此 API、飞书卡片、审计日志仍展示 `PLANNING_REQUIREMENTS`、`TESTING_CONFIRM_PENDING` 等可读状态。可观测性必须同时记录 `task_type + current_state`，不能只显示抽象的“执行中”。

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

## 统一循环模型（v1.1）

每个可迭代交付阶段都允许多轮执行，但必须区分三类循环：

1. **Agent 主动澄清**：Agent 无法在现有上下文下安全继续，发出 `ask_user`，进入对应的 `*_INPUT_PENDING`；用户 `answer` 后回到原阶段，并创建新 Task。
2. **用户要求修订**：产物已生成，但用户在 `*_CONFIRM_PENDING` 选择 `revise` 并提供反馈；系统保存反馈包后回到原阶段。
3. **下游发现上游问题**：测试、Review 或验收产生结构化失败反馈，进入 `REWORK`，由用户选择应回退的层级。

每次重新进入 Agent 阶段都增加该阶段的 `round_number`，不得覆盖旧 Task、旧产物版本或旧反馈。旧 approval 在状态离开时必须标记为 `expired`。

上游产物修订确认后必须执行 artifact impact analysis。受影响的下游产物标记为 stale，基于旧版本运行的 Task 结果必须丢弃；只有 impact report 已生成且回退目标明确后才能继续。切换状态本身不能替代失效传播。

### 多轮规划状态

| 执行态 | 主动澄清态 | 产物确认态 | `answer`/`revise` 返回 |
|---|---|---|---|
| `PLANNING_REQUIREMENTS` | `REQUIREMENT_INPUT_PENDING` | `REQUIREMENT_CONFIRM_PENDING` | `PLANNING_REQUIREMENTS` |
| `PLANNING_ARCHITECTURE` | `ARCHITECTURE_INPUT_PENDING` | `ARCHITECTURE_CONFIRM_PENDING` | `PLANNING_ARCHITECTURE` |
| `PLANNING_IMPLEMENTATION_SPECS` | `IMPLEMENTATION_SPEC_INPUT_PENDING` | `IMPLEMENTATION_SPEC_CONFIRM_PENDING` | `PLANNING_IMPLEMENTATION_SPECS` |
| `TESTING` | `TESTING_INPUT_PENDING` | `TESTING_CONFIRM_PENDING` | `TESTING` |
| `REVIEW` | `REVIEW_INPUT_PENDING` | `REVIEW_CONFIRM_PENDING` | `REVIEW` |

`confirm` 才进入下一阶段。输入回答和修订意见必须作为结构化 feedback packet 注入新一轮 Agent，而不是只保留一段 `latest_*` 文本。

### 质量门

| Agent 结果 | 含义 | 处理 |
|---|---|---|
| runtime/schema/merge failure | Agent 未形成可信业务结论 | 按技术失败重试；超过上限进入 `FAILED` |
| quality pass | 测试或 Review 明确通过 | 进入对应 `*_CONFIRM_PENDING`；可按 approval mode 自动确认 |
| quality fail | 测试失败或 Review 有 blocking issue | 进入 `REWORK`；禁止自动确认通过 |
| human revise | Agent 判断通过，但用户不同意 | 进入 `REWORK` |

禁止把 `TESTING` / `REVIEW` 的 `fail` 改写为 `complete`。自动模式只能自动确认已经由 Agent 判定为 pass 的质量门。

### REWORK 路由

`REWORK` 始终需要人工选择，系统可提供 `recommended_rework_target`，但不得自动替用户覆盖明确失败。通用目标为：

- `to_requirements`：需求不一致或需求缺失
- `to_architecture`：方案或系统边界错误
- `to_specs`：验收标准、接口契约或任务拆分错误
- `to_coding`：实现缺陷或漏实现
- `to_testing`：测试用例、测试数据或测试环境问题

`bug_fix` 和 `documentation` 任务仅展示其状态机实际支持的目标。

## 用户中途输入与命令

### 所有 Agent 的主动 Grill-Me 循环

`ask_user` 不是 Planner 专属事件。Planner、Coder、Tester、Reviewer、Acceptance Evaluator 和 Project Manager 都可以在完成当前 task 前进入可恢复输入边界。

```text
AGENT_EXECUTION
  ├─ complete → 下一质量门
  ├─ fail → 技术失败或 REWORK
  └─ ask_user → AGENT_INPUT_PENDING
                    ├─ answer/select_option → 原 Agent 新一轮
                    ├─ ask_user → 继续多轮输入
                    ├─ accept_default → 原 Agent 新一轮
                    ├─ pause/cancel → 控制状态
                    └─ scope_change → REWORK/上游阶段
```

每个 agent_execution 状态必须声明其 input pending 状态；可以使用阶段专属状态或统一 `AGENT_INPUT_PENDING`，但 payload 必须保存 `resume_state`、agent、stage、task_id 和 round。

问题至少包含 `question_id`、正文、提问原因、blocking、选项、推荐项、是否允许自由输入、默认假设和受影响追踪 ID。输入态允许连续多轮；blocking question 未解决或默认假设未获用户接受时禁止回到完成路径。

用户输入是独立于 Agent 输出的控制流，不得被当前执行吞掉。

### 命令通道

- `/status`、`/help`：只读，不改变状态。
- `/pause`：立即设置项目控制状态为 paused；正在运行的 Agent 可结束，但其结果在恢复前不得推进状态。
- `/resume`：恢复保存的执行状态，并先消费暂停期间的输入。
- `/cancel`：进入终止状态；之后到达的 Agent 结果全部丢弃。
- `/rollback`：记录目标状态；当前 Agent 完成后丢弃过期结果，从目标状态创建新轮次。
- `/confirm`、`/reject`、`/input`：只允许操作项目当前状态对应的唯一 pending approval。

### 自由文本通道

1. 当前为 `*_INPUT_PENDING`：文本作为该 approval 的回答。
2. 当前为 `*_CONFIRM_PENDING`：文本保存为对当前产物的评论；除非明确解析为 confirm/revise，否则不自动转换状态。
3. Agent 正在运行：文本追加到持久化 `pending_user_inputs` 队列，记录 `input_id`、时间、发送者、目标项目和当时状态。
4. 无运行任务且无 pending approval：作为项目追问处理；若包含范围变更，创建 feedback packet 并进入 `REWORK`，不得静默修改已确认需求。

Agent 完成后，Orchestrator 在合并产物和推进状态前必须重新读取项目控制状态及未消费输入：

- 存在 cancel/rollback/pause 时，优先执行控制命令。
- 存在影响当前产物的文本时，先进入确认或 REWORK，不得自动通过。
- 输入按创建顺序消费，并以 `consumed_by_task_id` 留下审计记录。

## 通用 Agent 提问子状态机

`ask_user` 是所有 agent-execution 状态的通用事件，不应为每个 Agent 复制一套固定状态。状态机引擎应支持参数化 `AGENT_INPUT_PENDING` 覆盖层，并持久化：

- `resume_state`、`origin_task_id`、`origin_round`；
- `question_id`、问题类型、阻塞级别、选项、推荐项及默认行为；
- `conversation/project` 绑定、已提交回答和受影响产物 ID。

通用轮转为：`S(agent_execution) → ask_user → AGENT_INPUT_PENDING(resume_state=S) → answer → S(new round)`。原 Task 以 `waiting_for_user` 或等价终态结束，回答后必须创建新轮次，不能让已退出的进程原地继续。用户回答若改变上游产物，则按影响分析跳转到 Requirements/Architecture/Specs/REWORK，而不是机械返回 `resume_state`。

Planner、Tester、Reviewer 的阶段输入态以及 Coder 的 `CLARIFICATION_PENDING` 都由同一 profile 编译规则生成，并复用 QuestionInteraction 协议。Acceptance Evaluator 和 Project Manager 仍需完成同等闭环；实现完成前，不得把这两者宣称为已交付能力。

并发约束：每个 `(project_id, origin_task_id, question_id)` 至多一个 pending interaction；服务重启和多会话投递均复用同一记录。收到回答时先以 CAS/事务完成 pending→answered，再恢复一次，防止两个聊天窗口重复触发。

## 按状态的事件语义

### Reject 交互流程

reject 必须按确认节点类型分流，不能用同一条“先消费 approval、再等自由文本”的路径。

**规划确认（需求 / 方案 / Spec / Bug 分析 / 文档大纲）**

- 修改意见是重跑 Planner 的必要输入。
- 用户只点击「驳回」但尚未填写意见时，**不得先 resolve 原 approval**；系统展示驳回意见输入卡，项目仍停在原 `*_CONFIRM_PENDING`，原 approval 保持 pending。
- 用户提交意见后，在同一事务中创建 feedback packet、resolve approval(reject)、转换回对应 Planner 状态。
- `/reject appr_xxx feedback` 可一次性完成上述事务。
- 用户取消输入时保持原确认态，不能留下“执行态但没有 Task、也没有 pending approval”的隐式等待状态。

**质量确认和最终验收（Coding / Testing / Review / Acceptance）**

- 点击「驳回」即表示结果不被接受，反馈文本可以为空。
- 系统原子 resolve approval(reject) 并进入 `REWORK`。
- 进入 `REWORK` 后必须立即且仅创建一次 REWORK approval/选择卡；不得回复“请直接输入修改意见”后停住。
- 可选反馈若随命令到达则在同一事务创建；若稍后补充，则绑定当前 REWORK 和来源 Task，但不阻止回退选择卡出现。
- Acceptance 先由只读 `acceptance_agent` 从真实入口主动黑盒验证，再由独立 `acceptance_evaluator` 复核证据。normal/night/auto 证据完整且均判定通过时自动进入 `MERGING`；strict 保留人工终审。
- `system_observable` 验收项不得要求用户代测。只有 `user_preference`、`authorization`、`external_fact` 可以进入用户问题交互，并优先使用选择题。

**推进不变量**

1. 状态进入 `human_confirmation` / `human_input` 后，只允许一次 `ensure_pending_approval_and_card`；已有 pending approval 时复用，不得过期后重建。
2. 若推进函数开始时项目已经位于 human state，tick 一次创建/复用 approval 后必须返回，禁止再 tick 第二次。
3. 若从 agent/system state tick 后到达 human state，再 tick 一次创建 approval 后返回。
4. 每个非 terminal 状态在 handler 返回前必须满足之一：有 running Task、有 pending Approval、存在明确 paused 控制状态；否则视为 stuck invariant violation 并记录 ERROR。

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
- **`reject`**：回到 `PLANNING_REQUIREMENTS`，保留用户评论作为 planner 输入。交互流程见上方「Reject 交互流程」。

### `PLANNING_ARCHITECTURE` → `ARCHITECTURE_CONFIRM_PENDING`

- **事件**：`complete`
- **触发**：planner stage=architecture 成功输出 `ARCHITECTURE.json`。
- **副作用**：
  - 合并 `ARCHITECTURE.json`
  - 将完整技术方案渲染为汇报卡片
  - 发送仅包含“确认 / 驳回”的方案确认卡片

### `ARCHITECTURE_CONFIRM_PENDING`

- **`confirm`**：进入 `PLANNING_IMPLEMENTATION_SPECS`
- **`reject`**：回到 `PLANNING_ARCHITECTURE`。交互流程见上方「Reject 交互流程」。

需要用户选择的技术决策必须在 `PLANNING_ARCHITECTURE` 阶段通过 `ask_user` 进入 `ARCHITECTURE_INPUT_PENDING`。多个决策渲染为多道独立单选题，逐题选择后统一提交；回答完成后 Planner 重新生成完整架构方案。

### `PLANNING_IMPLEMENTATION_SPECS` → `IMPLEMENTATION_SPEC_CONFIRM_PENDING`

- **事件**：`complete`
- **触发**：planner stage=specs 成功输出 `SPECS.json`。
- **副作用**：合并 `SPECS.json`。

### `IMPLEMENTATION_SPEC_CONFIRM_PENDING`

- **`confirm`**：进入 `CODING`
- **`reject`**：回到 `PLANNING_IMPLEMENTATION_SPECS`。交互流程见上方「Reject 交互流程」。

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
- **`needs_spec_change`**：用户回答导致需要改 Spec，回到 `PLANNING_IMPLEMENTATION_SPECS`
- **`needs_architecture_change`**：用户回答导致需要改方案，回到 `PLANNING_ARCHITECTURE`
- **`needs_bug_analysis_change`**（bug_fix 任务）：回到 `BUG_ANALYSIS`
- **`needs_outline_change`**（documentation 任务）：回到 `DOC_OUTLINE`

### `CONFLICT_PENDING`

- **`resolve`**：用户裁决后继续，回到 `CODING`，裁决内容写入 `context.conflict_resolution`
- **`needs_spec_change`**：回到 `PLANNING_IMPLEMENTATION_SPECS`
- **`needs_architecture_change`**：回到 `PLANNING_ARCHITECTURE`
- **`needs_bug_analysis_change`**（bug_fix 任务）：回到 `BUG_ANALYSIS`
- **`needs_outline_change`**（documentation 任务）：回到 `DOC_OUTLINE`

### `TESTING`

Agent：`tester`

| 事件 | 含义 | 处理 |
|------|------|------|
| `complete` | 测试通过 | 进入 `TESTING_CONFIRM_PENDING` |
| `fail` | 测试质量失败 | 生成 feedback packet，进入 `REWORK`，禁止自动通过 |
| runtime/schema failure | 测试 Agent 未形成可信结论 | 在 `TESTING` 重试，超过技术重试上限进入 `FAILED` |

### `REVIEW`

Agent：`reviewer`

| 事件 | 含义 | 处理 |
|------|------|------|
| `complete` | Review 通过 | 进入 `REVIEW_CONFIRM_PENDING` |
| `fail` | Review 存在 blocking issue | 生成 feedback packet，进入 `REWORK`，禁止自动通过 |
| runtime/schema failure | Reviewer 未形成可信结论 | 在 `REVIEW` 重试，超过技术重试上限进入 `FAILED` |

### `ACCEPTANCE_PENDING`

- **`confirm`**：进入 `MERGING`
- **`reject`**：进入 `REWORK`，由用户根据反馈选择根因层级

按任务类型提供的回退选项：

- **`feature`**：
  - `reject_to_requirements`：回到 `PLANNING_REQUIREMENTS`
  - `reject_to_architecture`：回到 `PLANNING_ARCHITECTURE`
  - `reject_to_specs`：回到 `PLANNING_IMPLEMENTATION_SPECS`
- **`bug_fix`**：
  - `reject_to_analysis`：回到 `BUG_ANALYSIS`
- **`documentation`**：
  - `reject_to_outline`：回到 `DOC_OUTLINE`

> 用户驳回时，系统通过 `acceptance_evaluator` 或 `project_manager` Agent 生成根因分析，推荐回退阶段，但最终由用户选择。

非 strict 模式若主动验收与独立评估均通过，系统以 `system_auto` 触发 `confirm`；仍会发送验收证据汇报卡，但不发送多余确认卡。strict 模式以及确有外部输入缺口时继续保留人工边界。

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
   - Tester 技术执行失败：`MAX_TEST_RETRIES`（默认 2）
   - Reviewer 技术执行失败：`MAX_REVIEW_RETRIES`（默认 2）
   - 其他 Agent 技术执行失败：`MAX_AGENT_RETRIES`（默认 3）
4. **技术失败重试**：进程异常、超时、JSON/schema 无效或 merge 失败可重试；每次创建新 Task/worktree。业务质量 fail 不消耗技术重试次数，而是进入 `REWORK`。

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
   - 规划 `human_confirmation`：调用 `conversation_coordinator`（Codex）读取阶段产物并选择 confirm/reject；无证据或依赖用户偏好、授权、外部事实时升级用户。
   - 质量 `human_confirmation`：只有对应专业 Agent 已给出有效 pass 时自动 confirm；fail 必须进入 `REWORK`，Coordinator 不得自审改判。
   - `REWORK`：存在 blocking feedback 的合法 `recommended_rework_target` 时按最新证据自动回退；没有可追溯建议才升级用户。
   - `ACCEPTANCE_PENDING`：Acceptance Agent 与 Evaluator 证据充分且 pass 时自动交付；只有外部证据缺口才等待用户。
   - `human_input`（如 `CLARIFICATION_PENDING`）：
     - 调用 `conversation_coordinator` 基于确认事实和产物尝试回答
     - 以 `answer` 事件推进
     - 无法可靠回答且属于用户偏好、授权或外部事实时才发送用户问题

### `auto_resolve_strategy`

- `none`：不自动决策，必须人工确认。
- `conversation_coordinator`：由 Codex Coordinator 基于项目证据回答或审批；不得冒充用户。
- `project_manager_default`：旧项目兼容策略；新项目不再使用。

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
