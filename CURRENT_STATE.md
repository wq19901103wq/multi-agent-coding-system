# MAC System 当前状态

> 更新时间：2026-07-17（Asia/Shanghai）  
> 本文件只记录当前可用能力和最新验证结果；历史过程放入 `docs/reviews/`。

## 已实现

- 单一 `delivery.json` + feature/bug_fix/documentation workflow profile。
- Specify → Design → Plan → Coding → Testing → Review → Acceptance → Merge 主链路。
- ConversationSession、Conversation Coordinator、PendingConversationAction 和执行前确认。
- 项目内 Approval、FeedbackPacket、PendingUserInput、QuestionInteraction。
- Planner、Coder、Tester、Reviewer、Acceptance Evaluator 主动提问与原阶段恢复。
- Coder/Tester/Reviewer 结构化输出持久化，作为下游质量证据。
- Runtime 自动技术重试；耗尽后进入恢复确认态，可重试、回退或终止。
- Coder、Tester、Reviewer 使用有界执行提示；临时 worktree 禁止污染宿主 editable install。
- 被取消/暂停的项目会拒绝晚到 Agent 结果，不能被缓存对象重新推进。
- SQLite 使用 WAL、30 秒 busy timeout 和外键检查，降低 Agent 与聊天并发写冲突。
- 规划确认驳回先收集修改意见（驳回表单卡），再原子创建反馈并流转。
- 飞书 WebSocket、确认/汇报/问题/状态/恢复卡和 Control API。
- `full_feature_flow` 脚本可自动接管质量 Agent 主动 fail 产生的 REWORK（`wait_for_approval_at_state`，最多 3 层恢复）。

## 最新验证

- 完整单元测试：**246 passed**，2 个第三方弃用警告（2026-07-17 复跑确认）。
- Ruff lint 与 `git diff --check` 通过；仓库历史代码尚未整体满足 `ruff format --check`。
- 真实飞书 `conversation_delivery` 已通过：只讨论不创建、执行前确认、单次创建、重复点击幂等回执、取消清场。
- 真实飞书卡片场景已通过（2026-07-16/17）：
  - `planning_rejection_form`：驳回表单卡渲染、结构化反馈提交后回到 `PLANNING_REQUIREMENTS`。
  - `architecture_confirmation`：架构终审卡只暴露 confirm/reject。
  - `acceptance_question_card`：Acceptance ask_user 选择卡渲染、选项提交后恢复 `ACCEPTANCE_PENDING`。
  - `runtime_recovery_card`：恢复卡渲染、卡上终止回调使项目进入 `cancelled`。
- 真实飞书 `full_feature_flow` 全分支（2026-07-17 01:35–03:01，项目 `proj_20260717_013549_7aed`）：
  需求驳回、架构确认、Spec 驳回、编码 reject→REWORK→to_coding、测试 reject→REWORK→to_testing、
  评审 reject→REWORK→to_coding、验收 reject→REWORK→to_requirements 后全量重放，最终 `COMPLETED`。

## 尚未宣称完成

- Agent 主动 fail 触发的自动 REWORK 接管路径仅有单测与脚本逻辑覆盖；真实飞书运行中尚未自然出现 Agent 主动 fail（昨晚全部为脚本驱动的人工 reject）。
- SQLite WAL/busy-timeout 修改后的并发压力验证。
- Project Manager 不是独立交付阶段 Agent；控制类澄清由 Conversation Coordinator 负责。
- 多人群管理员权限、生产任务队列、生产数据库与完整可观测性。
