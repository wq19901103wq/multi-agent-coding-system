# Worktree 代码审查报告

> 日期：2026-07-11  
> 审查范围：`.worktrees/feat-core` 当前未提交实现  
> 审查方式：静态审查，未运行测试  
> 对照规范：状态机、多轮反馈、会话路由、产物追踪和验收相关 `specs/`

## 总结

当前实现已经加入阶段轮次、FeedbackPacket、PendingUserInput、测试/Review 失败进入 REWORK 等能力，但仍存在阻断真实 E2E 的问题：Agent Prompt 与 Schema 不一致、自由文本可能跨聊天串项目、Coding 期间范围变更无法阻止旧结果合并、Approval 响应与状态转换非原子。

建议在修复 P0 问题前，不要把当前 worktree 作为完整闭环版本合入主分支。

## P0：Planner、Tester、Reviewer、Acceptance Prompt 与 Schema 冲突

### 证据

- `prompts/planner_requirements.md` 仍要求字符串 goals/acceptance_criteria，最终示例是 Task envelope。
- `prompts/planner_specs.md` 仍要求 `assigned_agent`。
- `prompts/tester.md` 没有要求新的 `test_evidence`。
- `prompts/reviewer.md` 示例 issue 缺少 id、category、blocking。
- `prompts/acceptance_evaluator.md` 没有要求 acceptance_matrix 和 blocking_conditions。
- `agent_config.json` 将这些 Prompt 直接绑定到强化后的 `specs/schemas/*.json`。

### 影响

真实 Claude Runtime 使用 `--json-schema` 时，Agent 按 Prompt 示例输出将无法通过 Schema。Requirements 第一阶段即可失败；FakeRuntime 测试不会暴露这个问题。

### 根因

Schema 已升级为 REQ → AC → DEC → SPEC → TEST/REV → Acceptance Matrix，但运行时 Prompt 仍停留在旧的摘要结构；产物文件与最终 stdout/TASK output 的契约也没有清晰分离。

### 建议

1. 明确 Planner 最终 stdout 是 artifact 本身还是 Task envelope，禁止同一个 schema 同时承担两种结构。
2. 同步所有 Prompt 字段、示例和 allowed next_state_event。
3. 增加一组经过生产 Schema Validator 的 Prompt contract tests，不只使用 FakeRuntime。

## P0：群聊和私聊自由文本可能跨项目串话

### 证据

`src/mac_system/feishu/handlers.py::_find_active_project_for_user` 只根据 open_id 找绑定项目，再按 `Project.updated_at DESC` 返回最近 active 项目，没有使用当前 chat_id。

自由文本处理直接调用该函数并创建 PendingUserInput。

### 影响

用户在群 A 发送的补充需求，可能进入其私聊 B 中最近更新的项目；同一群内多用户、多项目也无法维护独立上下文。

### 根因

现有模型只有 ProjectUser 权限绑定和 Project 上单一 feishu_chat_id，没有 ProjectChat、ChatUserContext 和角色权限。

### 建议

1. 实现 `ProjectChat(project_id, chat_id, relation, notification_level)`。
2. 实现 `ChatUserContext(chat_id, open_id, active_project_id)`。
3. 增加 `/projects`、`/use`、`/current`、`/leave-project`。
4. 删除“最近更新项目”回退；没有唯一上下文时展示选择卡片。
5. PendingUserInput 固化 source_chat_id。

## P0：Coding/Planning 期间的 blocking 用户输入不能阻止旧结果推进

### 证据

`AgentOrchestrator._consume_pending_user_inputs` 会将 scope_change/correction 转为 blocking feedback，但只在 TESTING/REVIEW 阶段修改 Agent output 为 fail。其他状态直接返回。

### 影响

用户在 CODING 期间追加范围后：输入被标记 consumed，旧代码仍可 merge 并进入 CODING_CONFIRM_PENDING；night/auto 模式还可能继续自动推进。Requirements、Architecture、Specs 期间的纠正也不会使当前产物自动失效。

### 根因

实现把“质量门失败”与“上游输入使当前结果过期”混为一类，仅给测试/Review 增加了失败转换，没有通用 stale-result 处理。

### 建议

1. Agent 返回后、complete_task/merge/transition 前查询 blocking inputs。
2. 任意阶段存在 scope_change/correction 时，将当前 Task 标为 stale/discarded，不合并产物。
3. 生成 impact report 并进入对应 REWORK/规划输入态。
4. 只有普通 question 可以不使结果失效，但必须有明确回答/投递路径。

## P1：Approval 响应和状态转换仍然不是原子事务

### 证据

`ProjectService.respond_approval` 使用 CAS 更新 Approval，但随后 Feishu handler 和 Control API 分别调用 `transition_state`，两者各自 commit。

`respond_approval` 未校验：

- approval.state 是否等于 Project.current_state；
- action 是否属于 approval options；
- 请求 project_id 是否匹配 approval.project_id；
- actor 是否拥有对应角色权限。

CAS 失败时函数返回已经响应的 Approval，调用方仍可能继续执行自己的 transition。

### 影响

并发点击、重复请求或旧卡片可能导致成功反馈与实际状态不一致、审批已消费但状态未变，或发生二次/非法转换。

### 建议

实现真正的 `resolve_approval` 事务：

1. 锁定/条件更新 pending approval。
2. 校验 project、state、action、actor role。
3. 响应 Approval、过期其他 Approval、转换 Project 状态并记录 EventLog。
4. 单事务提交后再发送反馈卡片。
5. 返回 `won` / `already_resolved`，失败方不得再次 transition。

## P1：重复 tick 可以创建多个 pending Approval

### 证据

`ProjectService.create_approval` 每次直接插入新记录，没有复用同状态 pending Approval，也没有数据库唯一约束。

### 影响

API、飞书回调和后台 tick 并发时，同一项目状态可能出现多个有效 approval_id 和多张可操作卡片。单 approval CAS 无法解决多个 approval 记录之间的竞争。

### 建议

- 业务规则：同一项目最多一个 pending Approval。
- 数据库增加可实现的唯一约束/锁策略。
- 创建前复用同状态 Approval；状态不同则先过期旧记录。

## P1：Pause 会留下 running Task

### 证据

Agent 返回后，如果 Project.status 为 paused，Orchestrator 在 `complete_task` 之前直接 return。注释声称保留 completed Task，但实际 Task 仍是 running。

### 影响

恢复后系统可能认为已有运行任务而不创建新 Task，导致项目永久卡住；服务重启又会把它当 stale running task 失败处理。

### 建议

明确暂停结果策略：

- `completed_deferred`：保存结果，恢复后重新做 stale/input 检查再处理；或
- `discarded_paused`：丢弃结果，恢复时创建新 Task。

不得让已经退出的进程对应 Task 保持 running。

## P1：PendingUserInput 在业务成功前被永久消费

### 证据

Orchestrator 在 merge/transition 前调用 `consume_pending_user_input`；该方法立即 commit `status=consumed`。之后 feedback 创建、merge 或 transition 仍可能失败。

### 影响

服务崩溃或后续业务失败时，输入已经消失在 pending 查询中，恢复后不会再次处理。

### 建议

- 引入 `pending → processing → consumed/failed` 状态。
- 输入 claim、feedback 创建、Task 处置和状态转换使用事务或 outbox/saga。
- processing 超时后允许恢复重试。

## P1：Feedback resolve 使用伪造 Task ID

### 证据

`AgentOrchestrator._resolve_verified_feedback` 调用：

```python
resolved_by_task_id="quality_gate_verification"
```

但 `FeedbackPacket.resolved_by_task_id` 是指向 `tasks.id` 的外键。

### 影响

开启 SQLite foreign keys 后更新失败；未开启时产生无法追踪的悬空 ID。

### 建议

传入真实 Tester/Reviewer Task ID，并记录验证 evidence。

## P1：Feedback 可以被错误 Agent 或错误项目关闭

### 证据

系统根据 Agent 输出的 `verified_feedback_ids` 直接查 ID 并标记 resolved，没有校验 feedback.project_id、允许验证的阶段、原 blocking 状态或验证证据。

### 影响

错误或恶意输出可以关闭其他项目/阶段的反馈；Coder 尚未处理的 blocking issue 也可能被直接关闭。

### 建议

resolve 时校验：

- feedback.project_id == current project；
- verifier Task 属于当前项目；
- verifier agent/stage 有权验证该 feedback kind；
- Coder addressed_feedback 与 Reviewer/Tester evidence 完整；
- disputed 不得自动变为 resolved。

## P1：产物追踪与失效传播尚未实现

### 证据

代码已有 Task.round_number，但没有 Artifact、ArtifactVersion、TraceLink、ImpactReport 或 stale 状态；Task 也没有 based_on_artifact_versions/result_must_be_discarded。

### 影响

Requirements v2 改变后，系统无法确定哪些 Architecture Decision、Spec、测试和审批失效，只能依靠粗粒度状态回退。

### 建议

按 `specs/artifact_traceability_spec.md` 实现 artifact/version/trace links 和影响分析。状态转换不能替代依赖失效。

## P2：新增测试没有覆盖真实 Schema 与关键竞态

### 证据

新增 FakeRuntime 测试返回旧结构：Planner 使用字符串 AC，Tester 缺少 test_evidence。FakeRuntime 不走生产 Runtime 的 Schema 校验。

尚缺：

- Prompt/Schema contract tests；
- 两个不同 Approval 并发创建；
- 同一 Approval 双响应；
- pause 后恢复；
- 输入消费后 merge 失败；
- 多聊天串话；
- Feedback 跨项目关闭；
- 服务重启恢复。

## 推荐修复顺序

1. 同步所有 Agent Prompt、stdout/TASK/artifact 契约与 Schema。
2. 通用化 blocking input → stale/discard，阻止 Coding/Planning 旧结果 merge。
3. 实现原子 resolve Approval 和单 pending Approval。
4. 修复 pause Task 状态与 PendingUserInput 消费事务。
5. 实现 ProjectChat、ChatUserContext 和角色权限，删除最近项目回退。
6. 强化 Feedback resolve 权限、真实 Task ID 和验证证据。
7. 实现 ArtifactVersion、TraceLink 和 ImpactReport。
8. 增加真实 Schema、竞态、恢复和多会话测试。

## 2026-07-12 Reject 卡住补充结论

当前 reject 卡住有两个直接触发器：质量关无反馈 reject 进入 REWORK 后未创建 REWORK approval；带反馈 reject 从 REWORK 开始推进时连续 tick human state 两次，第一条 approval 被第二次过期。根因不是 Agent 慢，而是等待边界和 human handler 缺乏幂等语义。修复标准以 `specs/state_machine_semantics.md`、`specs/orchestrator_spec.md` 和 `specs/implementation_plan.md` 的 Reject 章节为准。

## 验证说明

本报告为静态审查结论。本轮未运行单元测试、集成测试或飞书 E2E，因此没有宣称测试通过。修复完成后应按照上述顺序运行相关小范围测试，再运行全量测试和真实飞书场景。
