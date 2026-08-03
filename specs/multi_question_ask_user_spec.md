# 多问题 Ask-User 与回复卡片交互规范

> 版本：v1.4  
> 状态：选择题和问答题严格串行；选择题提交后才发问答题；问答题一次一张；驳回回复卡待补  
> 目标：任何业务 Agent 都能一次提出多个独立问题，用户回答后准确恢复发问的 Agent 和阶段。

## 1. 协议

Agent 使用统一的 `question_batch`，schema 为 `specs/schemas/agent_question_batch.json`：

```json
{
  "success": true,
  "next_state_event": "ask_user",
  "completion_reason": "resolution_requested",
  "blockers": [{
    "id": "rr_auth_scope",
    "category": "ambiguous_requirement",
    "summary": "权限范围尚未确定",
    "evidence": ["WorkItem 中没有已确认的权限边界"],
    "required_resolution": "明确采用团队级还是个人级权限范围",
    "preferred_resolver": "coordinator",
    "blocking": true,
    "affected_acceptance_criterion_ids": ["AC-AUTH-002"]
  }],
  "question_batch": {
    "batch_key": "requirements_round_2",
    "questions": [
      {
        "question_id": "q_auth_scope",
        "kind": "decision",
        "question": "权限范围采用哪一种？",
        "reason": "会改变验收边界",
        "allow_free_text": false,
        "options": [
          {"key": "team", "label": "团队级", "impact": "团队共享", "recommended": true},
          {"key": "personal", "label": "个人级", "impact": "仅本人可见", "recommended": false}
        ],
        "affected_trace_ids": ["REQ-AUTH-001"]
      },
      {
        "question_id": "q_policy_note",
        "kind": "blocking",
        "question": "请补充特殊权限规则",
        "reason": "无法从已确认产物推导",
        "allow_free_text": true,
        "options": [],
        "affected_trace_ids": ["AC-AUTH-002"]
      }
    ]
  }
}
```

不再新增一套 `ask_user.questions` 或把答案只存进 `project.context.qa_history`。权威数据是：

- `QuestionInteraction`：一批问题、发问 Agent、stage、round、`resume_state`；
- `Question`：稳定问题 ID、类型、选项、推荐项、影响追踪；
- `QuestionAnswer`：答案、展示文本、回答人和时间；
- `Approval`：当前输入边界及飞书卡片路由信息。

这里的 `ask_user` 是 Agent 输出 schema 的兼容名称。业务 Agent 实际请求对象始终是 Coordinator：Orchestrator 先让 Coordinator 根据当前 WorkItem 和 active confirmed facts 尝试回答，并校验其 `source_fact_ids`；只有无法回答或引用无效时才设置 `escalated_to_user=true` 并创建用户卡片。密钥问题只把 `env:VAR` 能力引用放入事实和 Prompt，原值由 Runtime 注入。

`ask_user` 不是凭证特例。任何无法满足 WorkItem 完成条件、且需要外部信息、能力、权限、依赖或范围决策才能解除的情况，都必须同时输出非空 `blockers`。每项 blocker 遵循 `resolution_request.json`；Runtime 和 Orchestrator 都会拒绝只有问题、没有 ResolutionRequest 的输出。

## 2. 卡片交互

- **选择题**：合并到一张交互卡片里，使用按钮展示选项；点击选项仅切换卡片上的选中状态，不会直接提交。
  - 已选项高亮为 primary，未选项为 default；未手动选择时推荐项高亮。
  - 用户调整完毕后点击 **提交答案** 统一提交所有选择题答案。未手动选择的题回退到推荐项。
  - **跳过未答** 仅当批次中存在 `non_blocking` 选择题时显示；只跳过 non_blocking 题，blocking/decision 必须回答。
  - 提交后原卡更新为「已提交」回执态：所有选项按钮禁用、最终答案高亮、标题变绿、底部标注「已提交，Agent 继续执行」。
- **问答题**：一次只发一张卡片，用户回复后再发下一张，避免一次堆出多张。
  - 回复通过 `parent_id` 精确路由到对应问题，记录后把原卡更新为「已回答」回执态（禁用按钮 + 显示回答摘要）。
  - `non_blocking` 问答题显示 **跳过** 按钮；blocking/decision 问答题无按钮，必须回复。
  - 也保留 `/input <approval_id> <回答>` 兼容入口。

## 3. 回复路由

飞书文本事件携带 `parent_id` 时，项目归属必须跟随原问题卡片：

```text
reply.parent_id
  -> pending Approval.payload.reply_message_ids
  -> Approval.payload.reply_message_to_question[parent_id]
  -> Question
  -> QuestionAnswer
```

这条路径优先于 active project，因此用户即使在多个项目间切换，回复旧卡片也不会答到错误项目。群聊回复仍执行项目权限校验。

若没有 `parent_id`：

- `/input` 按 approval_id 路由；
- 普通自由文本先由 Coordinator 判断它是否明确回答某个 pending clarification；若是则定向路由，否则作为新话题处理并保留旧问题；
- 多候选项目时先显示项目选择器，不由 LLM 猜测。

## 4. 恢复规则

- 所有回答逐条提交到 `QuestionAnswer`，服务重启后从数据库恢复。
- 选择题先合并到一张卡片提交，提交后原卡变为「已提交」回执态，此时才发送第一张问答题卡片。
- `QuestionInteraction` 只有在所有问题（包括 `non_blocking`）都已回答或显式跳过后才 resolve；`non_blocking` 不强制回答，但必须由用户主动跳过或回复后才能继续。
- resolve 后按 `resume_state` 回到原 Agent：Tester 回 Tester、Reviewer 回 Reviewer，不统一回 Coding 或 Planner。
- Agent 下一轮 Prompt 注入历史问答，避免重复提问。
- 同一 `batch_key + origin round` 的重复输出不得创建第二批有效问题；旧 approval 进入 expired。
- 多个 conversation clarification 可以并存，不能因为创建新问题或用户提出新话题就批量 expire。

## 5. 多轮原则

- 同主题、当前即可预见的问题应一次性合并，避免故意拆成多轮。
- 上一轮答案产生新的条件分支时，允许同一 Agent 再次 `ask_user`。
- 能从已确认需求、方案、实现计划、代码或测试唯一推导的问题不得打断用户。
- 只有影响范围、契约、风险、成本、数据、安全或验收结论的问题才应设为 blocking。

## 6. 驳回反馈

目标交互是点击“驳回”后发送一张可回复卡片，回复文本创建 `human_rejection` FeedbackPacket；也允许“跳过说明，直接驳回”。当前仍支持 `/reject <approval_id> <feedback>` 和点击驳回后直接发修改意见，回复卡片两步式交互尚未完成。

## 7. 验收标准

- 一批混合选择题和开放题能够完成并恢复原 Agent。
- 回复问题卡片时不受当前 active project 影响。
- 多次 tick 不重复创建 pending approval 或重复写答案。
- blocking 问题未回答时不能进入确认态。
- 服务重启后能继续未完成的问题批次。
- 不同聊天、不同项目的卡片回复不会串扰。
- 未获得 ProjectUser 权限、且不在项目绑定群中的用户，不能通过按钮或回复文本回答问题。
