# RFC: 用单一 Coordinator 循环替代交付状态机

状态：第一阶段已实施并通过真实飞书黑盒；旧项目迁移待完成  
日期：2026-08-02

## 结论

MAC 不再预先把项目编译成“需求 → 架构 → Spec → 编码 → 测试 → Review → 验收 →
返工”的固定流程。交付期间只运行一个循环：MAC 自己拥有的 Coordinator 读取当前目标
和最新事实，调用 Orchestrator 工具，工具把真实结果返回给 Coordinator。

这不是增加一套“简单/复杂任务分类”。系统不判断复杂度，也不按关键词选择流程。
复杂工作只是自然产生更多次 Coordinator 循环。

## 保留与删除

保留：

- 飞书私聊、卡片和消息投递 outbox；
- 会话与项目绑定、多项目切换；
- Project、Task、QuestionInteraction、EventLog、CardProjection 等持久化实体；
- coding-agent Runtime Router，支持 Codex、Claude Code、Kimi Code、Pi；
- Git worktree 隔离、合并、取消与服务重启恢复；
- 密钥能力注入、成本与 profiling；
- 独立、只读、真实入口验证。

删除：

- `delivery.json` 和 `workflow_profiles/*` 对下一步的决定权；
- REQUIREMENT、ARCHITECTURE、SPEC、CODING、TESTING、REVIEW、REWORK 等固定阶段跳转；
- “任务复杂度”分类器和关键字路由；
- 每个阶段各自的一套确认、失败回退和输入状态；
- Tester、Reviewer、Acceptance 对同一结果的流水线式重复验收。

## 直接参考 Codex 的代码结构，而不是重新解释理念

兼容性实现基线固定到本机 `codex-cli 0.144.1` 对应的 OpenAI Codex 仓库提交
`44918ea10c0f99151c6710411b4322c2f5c96bea`；同时对照当前 main 提交
`2b5bdcf67547860f2e5c5a605009a70026796b2b` 检查协议演进。以下不是类比，而是
对应源码中的具体机制：

- `codex-rs/core/src/session/turn.rs::run_turn`：同一 turn 内循环采样；只要模型调用了
  工具或收到新输入，就把结果写回会话并继续采样，不先产出一张完整任务 DAG；
- `codex-rs/core/src/stream_events_utils.rs::handle_output_item_done`：模型输出被统一解析为
  普通消息或工具调用；工具调用天然令 turn `needs_follow_up`；
- `codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs`：子 Agent 是 `spawn_agent`
  工具创建的独立 thread，支持不继承、继承全部或只继承最近 N 轮上下文；
- `multi_agents_v2/send_message.rs`、`followup_task.rs`、`wait.rs`：协调通过消息和 mailbox
  activity 完成，不依赖外层预声明依赖图；
- `request_user_input.rs`：源码明确拒绝非 root Agent 调用问用户工具；
- `codex-rs/app-server/tests/suite/v2/dynamic_tools.rs`：宿主可在 `thread/start` 注册
  `dynamicTools`，收到 `item/tool/call` 后执行工具并把结果返回同一 turn；
- `turn_steer.rs`：用户在运行中追加的输入进入当前 thread，而不是被解释成状态机跳转；
- `item/started`、`item/completed`、`turn/completed`：界面状态来自后端事件流。

因此，MAC 不再要求 Coordinator 每轮返回 `CoordinatorTurn.actions`，也不建立自己的
任务 DAG 调度器。Coordinator 是一个持续的原生 Agent session。MAC 作为宿主给它注册
少量工具；模型直接调用工具，工具结果回到同一 session，直到自然结束、等待用户或项目
完成。

MAC 不复刻 Codex 的单一执行后端，也不把 Codex 或 Claude Code 当作整个系统控制面。
Coordinator session 可以由不同 Provider 适配器承载，但所有适配器暴露同一组宿主工具：

```text
mac_spawn_agent(provider, task_name, message, context_fork, access)
mac_send_message(agent, message)
mac_wait_agent(timeout)
mac_list_agents()
mac_request_user_input(questions)
mac_report(message)
mac_complete_project(summary, evidence)
```

`mac_spawn_agent` 只创建一个有界子任务，不声明完整流程或固定阶段。`mac_` 命名空间
避免与 Codex 内建 `spawn_agent`、`request_user_input` 等工具发生注册冲突。并行来自 Coordinator
连续发出多个 spawn 调用；先后关系由 Agent 消息和真实完成事件表达。Codex Coordinator
可以启动 Claude Code，Claude Coordinator 也可以启动 Codex、Kimi Code 或 Pi。Provider
只实现 Agent Task，不拥有 MAC 的项目、会话或用户交互。

## 唯一控制循环

```text
trigger = 用户消息 | Agent 结果 | 验证结果 | 服务恢复

start_or_resume_coordinator_thread(project)
append(trigger)

while turn is active:
    event = coordinator.next_event()
    project_event_log.append(event)
    card_projection.apply(event)
    if event is tool_call:
        result = orchestrator.execute_tool(event)
        coordinator.return_tool_result(result)
    if event is waiting_for_user or turn_completed:
        stop
```

Coordinator 不输出“下一状态”，而是在对话中调用工具。`spawn_agent` 不是阶段名。
coding-agent 可以在一次任务中自行调查、规划、修改和自测；
其内部上下文管理和压缩由 coding-agent 负责。MAC 只提供最小可用初始上下文。

Orchestrator 只执行通用依赖规则：依赖已完成的任务才可运行；没有依赖关系的任务可
并行；可写任务不得同时修改同一个 worktree。它不理解“需求、架构、编码、测试”这些
业务阶段，也不根据错误文字选择责任人。

## 数据状态不是工作流状态机

Project 只保存生命周期和当前执行姿态：

- 生命周期：`active / paused / completed / failed / cancelled`；
- 执行姿态：`coordinating / running_agent / waiting_user / waiting_external / idle`。

Task 只保存：`pending / running / completed / failed / stale / cancelled`。
QuestionInteraction 只保存：`pending / answered / expired`。

前端卡片必须直接投影这些实体，不能维护第二份“展示状态”。Agent、系统或用户完成
选择后，原卡片必须显示选择者、选择结果和产生的真实状态。

## Agent 合同

### Coding Agent

输入只包含：

- 当前目标；
- 当前工作区；
- 与该目标相关的确认事实；
- 尚未解决的验证发现；
- 必要能力引用；
- 完成条件和禁止事项。

输出只包含：结果摘要、变更、已运行验证、阻塞请求和可复核证据。Coding Agent 不决定
全局下一步，也不直接询问用户；阻塞请求交给 Coordinator。

### Verifier

Verifier 独立、默认只读，从用户可见入口验证当前目标。它输出 `pass / fail / blocked`
以及原始复现证据。失败不包含“回退到哪个阶段”；Coordinator 根据证据决定下一动作。

### Coordinator

Coordinator 是唯一决策与用户交互 Agent。它不修改项目代码，不把专业失败强行改成
通过，也不伪造偏好、授权和外部事实。

根 Coordinator 的限制必须是 Codex 原生能力边界，不是提示词约定：创建 app-server
thread 时传 `environments: []`，使 shell、apply_patch、view_image 不进入该线程；同时
关闭 Codex 内建 multi-agent feature，确保所有委派都经过 MAC 的七个宿主工具。Coder、
Verifier 子线程保留各自所需的执行环境，但使用独立 Codex home，避免把操作者个人的
MCP、plugins 和 skills 混入项目；项目本身的 AGENTS 指令仍从工作目录加载。这样根线程
无法绕过 Agent registry、卡片投影、
幂等账本和成本记录直接操作项目。

## 多 coding-agent 后端

多后端是 Orchestrator 工具能力，不是流程分支。Coordinator 在 `run_agent` 中从系统
提供的实际可用 Provider 列表选择 Codex、Claude Code、Kimi Code 或 Pi；不能引用
不存在的 Provider，也不需要 MAC 编写“什么问题一定交给谁”的分类表。

每个 Provider 适配器尽量使用其原生 session、resume、事件流、权限和子 Agent 能力。
MAC 只归一化最外层事件：运行、工具进展、用户问题、审批、完成、失败、成本。不得把
Provider 内部工具轨迹重新拼成下一轮 Prompt，也不得在 MAC 中重做其上下文压缩。

## 参考项目完整迁移

“完整对齐某个参考项目”不是独立任务类型，也不触发专用状态机。Coordinator 在目标中
保留参考根目录和完整性约束，Coding Agent 负责从真实源代码、数据、Prompt、skills、
few-shot、工具和行为入口建立能力清单并实施。Verifier 根据源项目的真实行为探针检查
目标项目。不得把用户举出的姓名、回答或单个失败 case 写成硬编码。

## 会话、恢复与幂等

- Project 持久化 Coordinator 的 provider、native thread/session id 和最后消费的事件游标；
- Codex 使用 `thread/start` / `thread/resume`，运行中纠正使用 `turn/steer`；Claude Code
  使用 `--session-id` / `--resume` 以及 stream-json 输入；不得每 tick 创建一次性新会话；
- 一个项目同一时刻至多有一个可写 coding-agent Task；
- `mac_spawn_agent` 像 Codex 原生 spawn 一样先持久化并立即返回 running identity；Agent
  在后台执行，`mac_wait_agent` 由真实任务完成事件唤醒。多个只读 Agent 可并行，可写
  Agent 仍受 worktree 冲突约束；
- 非 Codex 后端也以持久化 `CoordinatorAgentSession` 作为身份边界。单个写 Agent 因额度、
  网络或调用预算失败时，`mac_send_message` 复用同一身份并收养原 worktree，不丢弃已落盘
  修改；若多个失败 worktree 各自包含互补成果，Coordinator 可以建立一个目标明确的整合
  Agent，但必须直接整合已知成果，不得重新执行能力盘点；
- 服务恢复时先重连/恢复 Coordinator session，再根据 Agent registry 修复运行状态；
- 根 turn 结束不能无条件把 Project 设为 idle；项目级执行状态由持久化 running Task 推导。
  若后台 Agent 在根 turn 已结束后才到达终态，MAC 以 durable completion event 自动唤醒
  Coordinator，等价于 Codex parent mailbox 的完成通知；
- 飞书事件 handler 的 HTTP client 是短生命周期资源。后台 Agent 和自动唤醒 turn 各自创建
  独立有界 client，终态卡不得复用已经退出的 handler bridge；
- 飞书 SDK 的单次消息 event loop 也是短生命周期资源。所有后台 Agent 与终态唤醒任务必须
  提交到 FastAPI lifespan 的常驻 service loop；handler loop 只负责提交工作，不能持有子进程
  的超时、结果持久化或卡片终态生命周期；
- provider thread 已失败时不继续恢复其可能损坏或过长的上下文；新建受限 thread，并从
  Project 目标、确认事实和持久化 Agent 摘要恢复最小必要上下文；
- 工具调用先按 `thread_id + turn_id + call_id` 持久化，再执行副作用；
- 写 Agent 合并后由宿主返回权威 `host_delivery` receipt（`merged`、branch tip、merge commit、
  canonical HEAD）。该 receipt 优先于 Agent 自述；Agent 即使声称“无法提交”，只要宿主已成功
  合并，Coordinator 就不得再启动恢复集成任务；
- 重放相同 call id 时返回已持久化结果，禁止重复 Agent、重复问题和重复卡片；
- 失败证据原样进入下一次快照，不通过错误关键词映射责任阶段。
- Codex app-server 的 newline-delimited JSON 单条响应可能超过 asyncio 默认 64 KiB；MAC
  使用 16 MiB 有界 StreamReader limit，保证长项目 thread/resume 不因 framing 上限失败；

## 迁移策略

不是重写飞书、数据库和 Runtime。迁移顺序为：

1. 引入 Coordinator session、事件协议和宿主工具执行循环；
2. 新项目切换到该循环；
3. 把卡片和恢复逻辑改为从 Project/Task/Question 实体投影；
4. 将现有活跃项目转换为一个目标和未解决问题集合；
5. 删除旧状态机、workflow profiles、阶段 prompt 和对应测试。

迁移期间可以读取旧项目产物提取目标，但不得让新循环继续执行旧状态跳转。

## 验收标准

- 普通修改默认只有 Coordinator → Coding Agent → Verifier；
- Verifier 失败后，失败证据直接进入下一次 Coordinator 决策；
- 用户未回答旧问题而提出新问题时，新问题正常回答，旧问题保持 pending；
- 服务重启不会丢 Agent 任务或重复发卡；
- 飞书卡片状态与后台实体一致；
- 多 Provider 可执行同一合同；
- 通过 MAC 将个人 Agent 对齐 `mac-rpa` 的完整能力和记忆，且没有针对姓名或示例问题的
  硬编码。

## 2026-08-03 第一阶段实施与验证记录

已完成：

- 新增持久化 `CoordinatorSession`、`CoordinatorToolCall`、
  `CoordinatorAgentSession`，根 Coordinator 使用 Codex app-server 原生
  `thread/start` / `thread/resume` / `turn/start` / `turn/steer`；
- 七个宿主工具全部落地，工具副作用以前置持久化和 native call id 去重；
- 飞书新建项目默认进入 `agent_loop`，不再编译旧交付阶段；
- Codex 子 Agent 使用可恢复的原生 thread；`send_message` 将失败证据发回同一个
  Coder/Verifier session；其他后端继续通过 Runtime Router 运行，并在 follow-up 时复用
  持久化 Agent 身份和保留 worktree；
- 可写 Agent 使用独立 worktree，完成后合并；项目完成强制要求至少一个可写 Agent 和
  一个独立只读 Agent；
- 子 Agent 运行卡、完成卡和状态卡由持久化 Task/Agent/Project 投影并更新同一飞书消息；
- Provider 的逐 token delta 和 rate-limit 通知不再写入项目事件日志；用户状态卡过滤
  provider 协议事件，只显示任务、验收和交付事实；
- CardKit 2.0 的消息历史 API 不返回卡片正文，真实传输 E2E 改用入站事件关联和
  provider message id 验证，桌面 UI 黑盒另行直接检查可见语义。

验证证据：

- 全量单元/集成测试：`753 passed`；
- 真实飞书项目 `proj_20260803_005931_4137068986cbe65d`：Codex Coder 首轮实现后，
  独立 Verifier 从真实 CLI 入口发现根目录导入失败；Coordinator 将完整证据发回原
  Coder session，修复后再发回原 Verifier session，最终完成；
- 真实飞书项目 `proj_20260803_014313_6bbd3399be450fc8`：Codex 完成实现，Claude Code
  独立只读验收通过，两张 Agent 进度卡和完成卡均有真实 provider message id，后台与
  卡片最终状态均为 `completed`；
- 飞书桌面客户端 T5：真实 @Code 发送 `/status`，可见状态为“交付完成”，执行链路明确
  展示 Codex 实现、Claude Code 验收和交付完成，不再显示 `tokenUsage/tool_call` 等内部
  协议事件；截图：`/tmp/mac-feishu-status-fixed2.png`。

尚未完成：

- 旧活跃项目还保留旧状态机，尚未转换为目标与未解决证据集合；
- `delivery.json`、旧阶段 prompt 和兼容代码尚未删除；
- 个人 Agent 与 `mac-rpa` 的完整能力/记忆迁移需要作为下一阶段在新循环上重新执行；
- Codex 订阅后端只返回 token/rate-limit 使用量而不返回美元账单；本轮 Claude Code
  可计费成本为 `$0.106587`，不能把 Codex 的 `$0` 误读为零资源消耗。
