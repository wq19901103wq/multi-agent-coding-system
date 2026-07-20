# MAC System 当前状态

> 更新时间：2026-07-20（Asia/Shanghai）
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

- 完整单元测试：**320 passed**，2 个第三方弃用警告（2026-07-20 复跑确认）。
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
- 最新代码真实飞书复验（2026-07-19 10:01–10:51，项目 `proj_20260719_020155_d373`）：
  覆盖规划选择题、需求/Spec 驳回、三个质量关 REWORK、验收回需求和全量重放，最终 `COMPLETED`；
  本轮日志无汇报卡发送失败、`NoneType`、数据库锁、Traceback、运行时失败或 401。
- 项目控制与 full flow 复验（2026-07-20）：
  - `active_task_cancel` 在 13.3 秒内终止运行中 Agent，数据库与运行时进程均清理完成；
  - `full_feature_flow` 项目 `proj_20260719_164305_f9de` 覆盖需求/Spec 驳回、编码/测试/Review REWORK、验收回需求全量重放并最终 `COMPLETED`；
  - Tester 在真实运行中主动 fail，脚本按推荐项自动选择 `to_testing`，重测后通过，主动 fail 自动 REWORK 路径已获得真实飞书验证；
  - 服务终端审计未发现汇报卡发送失败、`NoneType`、数据库锁、Traceback 或 401。
- `requirement_custom_question` 已真实验证：选择“我要修改”只更新原卡为等待补充，用户提交定向文本后才恢复需求规划。

## 已关闭的 2026-07-18 Live Issues

> 关闭时间：2026-07-19（Asia/Shanghai）

### Issue 1：API / control 端点 401

- 服务端 `CONTROL_API_TOKEN` 配置正确，依赖 `require_control_api_token` 工作正常。
- 现场验证：
  - 不带 `Authorization` / `X-Control-Api-Token` → `/projects/{id}` 返回 **401**；
  - 带正确的 `Authorization: Bearer <CONTROL_API_TOKEN>` → **200**；
  - `/admin/projects` 因 `.env` 未配置 `ADMIN_TOKEN`，目前无认证要求，始终 200。
- **根因**：调用方漏传 token 或 token 错误；不是服务端配置丢失。
- **状态**：已关闭；E2E 客户端统一携带 Control API token。

### Issue 2：私聊收到自由文本输入卡而非选择题

- 项目 `proj_20260718_102234_f42e` 的 planner 在 `PLANNING_REQUIREMENTS` 完成后，把三个“有默认假设但需用户确认”的项写进产物 `ambiguities`。
- Orchestrator 的 `_block_on_unresolved_ambiguities` 将其合成为 `question_batch`，但 `allow_free_text=true`、`options=[]`，最终渲染为自由文本输入卡。
- **根因**：planner 没有按规范把“需用户做决策”的模糊点走 `ask_user` + 带 `options` 的 `question_batch`；系统兜底也未把有默认假设的歧义转成选择题。
- **修复**：强化 planner 约束，并在 Orchestrator 兜底生成“接受默认 / 我要修改”选项；自定义修改采用两步交互。
- **状态**：已关闭；单测与真实 `requirement_custom_question` 场景均通过。

### Issue 3：planner 连续 401 失败导致项目进入恢复态

- 历史项目 `proj_20260718_102234_f42e` 曾进入 `PLANNING_REQUIREMENTS_RECOVERY_PENDING`。
- 失败任务 `task_20260718_123056_5646` 至 `task_20260718_123159_8b58` 均报 `API Error: 401`。
- **根因**：Claude CLI 自动加载 `~/.claude/settings.json` 中另一套 Kimi 端点和凭证，覆盖了 MAC 显式提供的 DeepSeek 环境。
- **修复**：所有 Runtime 命令使用 `--setting-sources "" --bare`，禁止用户级 settings 和插件污染项目运行时。
- **状态**：已关闭；同一配置的手动调用、Runtime 单测和最新真实 full flow 均通过，日志无 401；历史项目也已在重启恢复后成功推进到 `ARCHITECTURE_CONFIRM_PENDING`。

详见 `docs/reviews/2026-07-18-live-issues-handover.md`。

## 尚未宣称完成

- SQLite WAL/busy-timeout 修改后的并发压力验证。
- Project Manager 不是独立交付阶段 Agent；控制类澄清由 Conversation Coordinator 负责。
- 多人群管理员权限、生产任务队列、生产数据库与完整可观测性。
