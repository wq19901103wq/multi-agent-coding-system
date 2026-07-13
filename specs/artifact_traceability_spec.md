# 需求、设计、Spec、实现与验收追踪规范

> 版本：v1.0  
> 用途：保证最终交付可以从验收结果反向追溯到测试、Spec、架构决策和已确认需求。

## 追踪链

所有交付必须形成以下有向关系：

```text
Requirement (REQ)
  -> Acceptance Criterion (AC)
  -> Architecture Decision (DEC)
  -> Implementation Spec (SPEC)
  -> Implementation Evidence (CODE)
  -> Test Evidence (TEST)
  -> Review Issue/Verdict (REV)
  -> Acceptance Matrix
```

ID 在项目生命周期内稳定；修订内容时创建新 artifact version，不复用 ID 表示不同语义。删除需求时标记 superseded/removed，并触发下游影响分析。

## ID 规则

| 类型 | 格式示例 |
|---|---|
| Requirement | `REQ-CHAT-001` |
| Acceptance Criterion | `AC-CHAT-001` |
| Architecture Decision | `DEC-CHAT-001` |
| Spec | `SPEC-CHAT-001` |
| Test/Evidence | `TEST-CHAT-001` |
| Review Issue | `REV-CHAT-001` |
| Feedback | `FB-CHAT-001` |

ID 只表达身份，不携带轮次。轮次和版本使用独立字段。

## Artifact Envelope

Requirements、Architecture、Specs 都使用共同元数据：

```json
{
  "artifact_id": "requirements",
  "version": 3,
  "round": 3,
  "status": "confirmed",
  "supersedes_version": 2,
  "based_on_artifact_versions": ["requirements@2"],
  "source_input_ids": ["input_12"],
  "content_hash": "sha256:..."
}
```

状态：`draft`、`pending_confirmation`、`confirmed`、`stale`、`superseded`、`rejected`。

只有 confirmed 且非 stale 的上游产物可以作为自动进入下一阶段的依据。

## 需求结构

- requirement：用户价值、行为或约束，带 stable ID、priority、source 和 status。
- acceptance criterion：可验证断言，带 stable ID、requirement_ids、verification_method。
- non-functional requirement 同样使用 REQ/AC，不允许只留在设计说明中。
- assumption 必须带 owner、risk 和 validation_plan；默认假设不能静默成为 confirmed requirement。

## Architecture 追踪

每个 Architecture Decision 必须引用 affected_requirement_ids / affected_acceptance_criterion_ids，并记录：候选方案、选择、理由、约束、风险和撤销条件。

架构产物还必须定义数据模型、接口/事件契约、事务与幂等边界、失败恢复、安全边界、迁移和可观测性。不能只列模块和文件。

## Spec 追踪

每个 Spec 必须引用 requirement_ids、acceptance_criterion_ids 和 decision_ids，并包含：

- preconditions / invariants
- interfaces / data_changes
- algorithm_or_behavior
- error_cases / security_checks
- migration_steps / rollback_plan
- test_cases
- observability
- definition_of_done
- dependencies（只能引用其他 Spec ID，必须构成 DAG）

`affected_files` 是预估，不是完成证据。Spec owner 分为 implementation、verification 和 review，不能用单一 assigned_agent 三选一。

## 失效传播

上游 artifact 新版本确认后，Orchestrator 执行影响分析：

1. 对比变更的 REQ/AC IDs 和内容 hash。
2. 标记引用已变更项目的 DEC/SPEC/Task/Test/Review 为 `stale`。
3. 使基于 stale artifact 的 pending approvals 过期。
4. 正在运行的 Task 标记 `result_must_be_discarded=true`；完成后不得 merge/transition。
5. 生成 impact report，由用户确认回退层级和可复用产物。
6. 未受影响的下游产物可以保留，但必须记录重新验证结果。

删除或放宽需求也必须触发影响分析，防止旧测试和实现继续约束已经变化的范围。

## 验收矩阵

最终验收必须逐条覆盖所有 active AC：

| AC | Requirements | Specs | 实现证据 | 测试证据 | Review | 结果 |
|---|---|---|---|---|---|---|

以下任一条件成立时，Acceptance Evaluator 不得建议 confirm：

- active AC 没有 Spec；
- Spec 没有 implementation evidence；
- AC 没有测试或手工验证证据；
- blocking review issue 未关闭；
- 使用了 stale/superseded artifact；
- 有未消费 scope-change/correction 输入；
- 测试环境失败导致结论 unknown；
- 迁移或回滚要求未验证。

## Spec 拆分质量门

一个 Spec 必须可在单个实现循环中完成并独立验证。建议阈值：

- 修改不超过 5–8 个主要文件；
- 至少映射一个 AC；
- 至少一个成功场景和一个相关失败/边界场景；
- 不同时混合大规模数据迁移、核心业务逻辑和多端 UI；
- dependencies 无环；
- 不能仅以“重构/优化”描述，必须给出可观察结果。

超出阈值时 Planner 必须拆分，或明确记录无法拆分的架构原因。
