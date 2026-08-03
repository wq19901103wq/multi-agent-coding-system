# Schema 分层与迁移状态

## 分层

- `agent_turn_output.json`：所有 Agent stdout 的公共控制 envelope。
- `agent_question_batch.json`：`ask_user` 时由 Agent 输出的问题批次；Agent 提供稳定 `batch_key`，Orchestrator 创建数据库 `interaction_id`。
- `requirements.json`、`architecture.json`、`specs.json` 等：工作区 artifact schema，不承担状态机控制输出。`requirements.json` 同时承载产品角色、流程、业务规则和功能行为，不再单独生成 Product Spec。
- `coder_output.json`、`tester_output.json`、`reviewer_output.json`、`acceptance_agent_output.json`、`acceptance_evaluator_output.json`：阶段业务结果 schema。Acceptance Agent 主动执行黑盒验收，Evaluator 独立审查证据并决定是否需要 Coordinator 向用户提问。
- `acceptance_evaluator_output.json` 的人工验收项使用 `manual_verification` 明确记录验证状态、证据来源、实际覆盖范围和限制。`system_observable` 接受 Acceptance Agent 的直接无保留黑盒证据；只有偏好、授权或外部事实才接受逐 AC 问题中用户明确选择 `verified_pass` 的结论。
- `task.json`：`.mac/TASK.json` 的公共封装，只校验公共 turn envelope；具体阶段结果仍需按 agent config 指定的业务 schema 二次校验。

## 当前实现

Planner stdout 已统一使用 `agent_turn_output.json`，产物另用对应 artifact schema 校验；Coder、Tester、Reviewer、Acceptance 的业务 schema 均以条件分支支持 `ask_user`。所有 `ask_user` 输出必须同时携带非空 `question_batch`、`completion_reason=resolution_requested` 与至少一个通用 `ResolutionRequest` blocker。Runtime 对最终 JSON 做 fail-closed schema 校验，Orchestrator 再执行状态有效性、WorkItem 完成条件、质量证据、反馈关闭状态和 context version 等语义校验。
