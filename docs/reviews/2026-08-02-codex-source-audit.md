# Codex 源码对照审计：MAC 动态工作流基线

日期：2026-08-02  
Codex 运行兼容基线：`rust-v0.144.1`，提交
`44918ea10c0f99151c6710411b4322c2f5c96bea`  
Codex main 对照提交：`2b5bdcf67547860f2e5c5a605009a70026796b2b`

## 审计结论

MAC 之前新增的 `CoordinatorTurn.actions + depends_on` 不是 Codex/Claude Code 式动态
工作流，而是把旧阶段状态机换成了模型生成 DAG。它仍然让外层系统负责拆任务、等待、
拼上下文和再次调用 Coordinator，复杂度没有真正消失。

正确基线是：持续的原生 Agent session + 宿主工具 + 事件流。Coordinator 直接调用工具，
工具结果返回同一 turn；MAC 不解析一张完整流程图，也不根据失败文本决定下一阶段。

## Codex 的具体实现

| 问题 | Codex 源码 | 可验证行为 | MAC 应采用的结构 |
|---|---|---|---|
| 一轮如何继续 | `codex-rs/core/src/session/turn.rs::run_turn` | `needs_follow_up` 为真时，同一 turn 再次采样 | Coordinator 保持 session，不按 tick 重建 prompt |
| 工具如何进入循环 | `stream_events_utils.rs::handle_output_item_done` | 工具调用持久化，执行结果写回 history，然后继续 | 工具调用/结果是事件，不是“下一状态” |
| 工具并行 | `tools/parallel.rs::ToolCallRuntime` | 支持并行的工具拿读锁，其余工具串行 | 由工具能力声明并发安全，不由任务阶段判断 |
| 子 Agent 创建 | `tools/handlers/multi_agents_v2/spawn.rs` | 创建独立 thread；支持 `none/all/最近 N 轮`上下文继承 | MAC 的 `spawn_agent` 需要显式 context fork |
| 子 Agent 沟通 | `send_message.rs`、`followup_task.rs` | mailbox 投递；需要时触发子 Agent 新 turn | 反馈直接发给原 Agent session，不创建 rework 阶段 |
| 等待子 Agent | `multi_agents_v2/wait.rs` | 等待 mailbox activity；用户 steer 会打断等待 | 事件唤醒，不轮询 Task DAG |
| 问用户 | `request_user_input.rs` | 非 root Agent 调用会被拒绝 | 只有 Coordinator 暴露问用户工具 |
| 用户中途纠正 | `app-server/.../turn_steer.rs` | 输入进入当前活跃 turn | 私聊新消息先作为 steer，不映射成固定状态跳转 |
| 宿主扩展工具 | `app-server/.../dynamic_tools.rs` | `thread/start.dynamicTools` 注册；`item/tool/call` 请求/响应 | MAC 将多后端 Agent 和飞书交互作为宿主工具 |
| 前端状态 | `item/started`、`item/completed`、`turn/completed` | UI 消费统一生命周期事件 | 飞书卡片只投影后台事件，不维护第二套状态 |
| 上下文过长 | `session/turn.rs` 的 pre/mid-turn compaction | 压缩后仍继续同一 session | 不把每轮工具日志手工重拼到新 prompt |

## 当前 MAC 的具体偏差

1. `orchestrator.py` 同时承担状态跳转、上下文拼装、失败路由、卡片发送和重试，等于把
   Codex 中 session loop、tool router、event projection 四层揉成一个类。
2. 原型 `dynamic_workflow.py` 要求模型一次性产出 `actions` 和 `depends_on`，再由 MAC
   判断 ready task；这是自建调度器，不是原生 Agent loop。
3. `CodexRuntime` 使用 `codex exec --ephemeral`；每个 MAC Agent task 都丢弃 Codex
   thread，无法原位 follow-up、steer 或让 Codex 自己压缩上下文。
4. Claude Code 每次 `execute` 也启动新进程，没有持久化并恢复 `session_id`；Pi 显式
   使用 `--no-session`。这些配置让 MAC 被迫手工携带越来越大的历史摘要。
5. Agent 反馈被转换成 REWORK/阶段回退，而不是用原始证据 `send_message` 给原 Agent。
6. 飞书卡片有部分由流程分支主动发送，不完全来自统一事件投影，因此后台已选择而
   卡片仍未更新的情况会反复出现。

## 实施约束

- 不把 Codex 嵌入为唯一控制器；Codex app-server 只是 Coordinator runtime adapter。
- Claude Code、Codex、Kimi、Pi 都通过相同的 MAC host-tool contract 启动和通信。
- 第一版工具集保持小：`mac_spawn_agent`、`mac_send_message`、`mac_wait_agent`、
  `mac_list_agents`、`mac_request_user_input`、`mac_report`、`mac_complete_project`。
- 不引入任务类型分类器、失败关键词路由或完整 DAG schema。
- 先让新 session loop 在单个受控项目跑通，再将飞书新项目切换；未通过真实 E2E 前，
  不能让不完整原型成为默认执行路径。

## 已完成的纠偏

- RFC 已删除 `CoordinatorTurn.actions` 作为目标合同，改为原生 session/tool/event loop。
- 新增 `CodexAppServerCoordinator`：直接实现 `initialize`、`thread/start`、
  `thread/resume`、`turn/start`、`turn/steer`、`item/tool/call` 响应和 turn 事件消费。
- 已用协议级单测证明：宿主工具结果返回同一 turn；thread 可恢复；用户输入可 steer；
  动态工具在 thread 创建时注册。

真实本机 `codex app-server` 首次探针发现：模型尝试使用 dynamic tool 时连续报告
`local execution-host error`，MAC 收不到 `item/tool/call`。根因是 Codex 0.144.1 在
macOS 上把外部工具路由到无法正常启动的 code-mode host；这也解释了仓库原
`CodexRuntime` 为什么显式 `--disable code_mode_host`。Coordinator adapter 采用同一
配置后再次调用真实 API，成功收到 `lookup_ticket({ticket_id: "MAC-42"})`，返回
`{"status":"resolved"}` 后，同一 turn 最终回答 `Ticket MAC-42 is resolved`。

这证明 app-server 的原生动态工具闭环在当前安装版本可用。

## 2026-08-03 落地复核

上述待办已经在飞书新项目主链完成：七个宿主工具、native session/call id 持久化、
同一 Codex 子 Agent session follow-up、worktree 隔离与合并、独立只读验收、恢复入口、
NotificationOutbox/CardProjection 和飞书状态卡均已接入。真实飞书测试证明 Coordinator
可以把 Verifier 的失败证据直接发回原 Coder，再让原 Verifier 复验，无 REWORK 阶段。

第二个真实项目由 Codex 实现、Claude Code 验收，说明 Coordinator 不是 Codex 的单后端
包装。桌面客户端最终状态卡与后台 `COMPLETED` 一致。旧活跃项目与旧阶段兼容层仍在，
所以本结论只覆盖飞书新建 `agent_loop` 项目；删除旧状态机必须等旧项目迁移完成。

## 2026-08-03 根 Coordinator 工具边界复核

个人 Agent 真实项目暴露了一个协议边界遗漏：注册七个 `dynamicTools` 并不代表 Codex
只会看到这七个工具。根 Coordinator 仍继承默认 execution environment，因此绕过
`spawn_agent` 使用了内建 `commandExecution`，直接扫描 2.1 GB 参考数据，最终因单块输出
超过 app-server 限制而失败。提示词里的“必须委派”无法形成能力隔离。

修复直接采用 `rust-v0.144.1` 的实现，而不是新增 MAC 工具过滤器：

- `app-server-protocol/src/protocol/v2/thread.rs::ThreadStartParams.environments` 明确规定：
  省略字段时选择默认环境，空数组时关闭该 thread 的环境访问；
- `core/src/tools/spec_plan.rs::add_shell_tools` 在没有 environment 时直接返回，因此不会
  注册 `exec_command`、`write_stdin` 或 shell；同一文件对 apply_patch/view_image 也以环境
  存在为前提；
- `features/src/lib.rs` 显示 Codex 内建 `multi_agent` 默认开启，所以根线程还需通过
  thread config 的 `features.multi_agent=false` 和 `features.multi_agent_v2=false` 关闭，
  统一使用 MAC 可持久化、可投影、可计费的 Agent 工具。

MAC 现在仅给根 Coordinator 发送 `environments: []`，Coder/Verifier 不发送该覆盖，继续
使用 Codex 的完整编码环境。真实 app-server 探针只产生一个 `dynamicToolCall`，没有
`commandExecution` 或 Codex 内建子 Agent 事件。失败的 provider thread 不再 resume，
系统以项目目标、确认事实和持久化 Agent 摘要重建一个受限的新 thread。

随后真实恢复又证明 `environments: []` 不会移除用户全局 CODEX_HOME 提供的 MCP；旧线程
仍可通过 `node_repl` 读取文件。根 Coordinator 因此改用独立最小 CODEX_HOME，只链接登录
凭据，不继承全局 config、MCP、plugins、skills 或 AGENTS 指令；该能力配置带版本，部署后
旧线程自动轮换。七个动态工具统一使用 `mac_` 前缀，避免与 Codex 内建同名工具冲突。

真实个人 Agent 迁移还验证了失败连续性：写 Agent 因调用预算或上游连接中断后，文件已在
worktree 中落盘。MAC 现在让非 Codex 后端的 `mac_send_message` 继续同一个持久化 Agent，
并把新 Task 收养到原 worktree；只有当两个保留 worktree 含互补成果时，Coordinator 才创建
独立整合 Agent。这样保留了 Codex 的稳定 Agent 身份和异步等待语义，也不把不同分支的整合
强行塞进已经损坏或过长的 provider thread。

同一真实流程还暴露了三个宿主生命周期问题，现已统一按 durable state 修正：运行中的
Agent 拒绝并发 continuation；根 turn 结束时 Project 状态从 running Task 推导，并在后台
终态到达后自动唤醒空闲 Coordinator；后台卡片使用独立 Feishu client，不持有事件 handler
退出后已关闭的 bridge；后台 Agent 和 wakeup 统一归属 FastAPI lifespan 常驻 event loop，
不再挂在飞书 SDK 的单次消息 loop 上。另将 Codex app-server 子进程的 newline JSON 读取上限从 asyncio
默认 64 KiB 提升为有界 16 MiB，长 thread 恢复不再触发 `LimitOverrunError`。

后续 R6 复验又补齐了两处 Codex 式宿主语义。第一，只读验收不再等同于“操作系统层面
禁止一切写入”：Codex Verifier 在一次性隔离 worktree 中使用 `workspace-write` 运行真实
pytest、临时数据库和构建步骤，但该 worktree 永不合并，结束后强制销毁，因此交付边界仍
是只读。第二，子 Agent 完成信号按 project 合并并带 generation；若父 turn 尚在运行，
wakeup 等到父线程 idle 后再恢复同一 Coordinator session。父 turn 期间又有完成事件时，
generation 变化会保证至少再运行一次 reconcile，避免“后台全完成、协调器却停在 idle”。

编码 Agent 的环境发现也改成有界合同：优先仓库配置、目标中显式路径、`PATH` 和窄范围
探针，禁止为了寻找解释器或依赖递归扫描用户主目录、系统根和无关仓库。该规则约束的是
资源与上下文边界，不按命令字符串或失败 case 写关键词拦截。
