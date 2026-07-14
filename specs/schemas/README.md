# Schema 分层与迁移状态

## 分层

- `agent_turn_output.json`：所有 Agent stdout 的公共控制 envelope。
- `agent_question_batch.json`：`ask_user` 时由 Agent 输出的问题批次；Agent 提供稳定 `batch_key`，Orchestrator 创建数据库 `interaction_id`。
- `requirements.json`、`architecture.json`、`specs.json` 等：工作区 artifact schema，不承担状态机控制输出。`requirements.json` 同时承载产品角色、流程、业务规则和功能行为，不再单独生成 Product Spec。
- `coder_output.json`、`tester_output.json`、`reviewer_output.json`、`acceptance_evaluator_output.json`：阶段业务结果 schema；落地时应作为 turn envelope 的 `stage_result` 分支组合校验。
- `task.json`：`.mac/TASK.json` 的公共封装，只校验公共 turn envelope；具体阶段结果仍需按 agent config 指定的业务 schema 二次校验。

## 当前迁移缺口

当前 Runtime 仍把部分 Planner artifact schema 直接作为 stdout `--json-schema`，Tester/Reviewer/Acceptance 的旧业务 schema 也把完成字段设为无条件必填，因此尚不能执行统一 `ask_user`。实现必须先完成“turn envelope 与 artifact/stage result 分离”，再切换 agent config 和 prompt；在此之前，全业务 Agent 主动提问仅是目标设计。
