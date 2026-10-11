# AGENTS.md — MAC 开发纪律

## 当前架构（2026-08-04）

MAC 是 coding agent 的移动端项目宿主，不是另一套 Agent 工作流框架。

- 一个 MAC 项目绑定一个持久 Project Lead thread。Project Lead runtime 使用统一生命周期接口，
  当前默认注册 Codex adapter，但项目执行循环不得直接写死具体 provider。
- 普通消息原样进入该 thread；运行中追加消息使用 runtime 的 `steer`，Codex adapter 必须保持
  Codex 原生 `turn/steer` 语义。
- 默认 Codex Project Lead 从首轮起拥有正常 coding tools、skills、上下文压缩和原生 multi-agent v2。
- 用户问题和审批使用 app-server 原生 `item/tool/requestUserInput`、`item/commandExecution/requestApproval`、`item/fileChange/requestApproval`、`item/permissions/requestApproval`，并始终先投影原问题/审批卡。普通真人消息不经过 Owner 前置分类或固定对齐阶段，直接进入 Root；Root 可按需读取项目内带来源的用户原则 `ProjectPolicy` 和未完成决定，发现会改变用户可感知结果、授权、费用、外部事实或不可逆边界的未决事项时使用原生提问。Owner 启用时，每个原生问题/审批先由独立 Owner 根据用户历史尝试代答，不能可靠代答时原卡继续等待用户。每轮完整结果汇报成功展示后仍必须触发一次 Project Owner 审阅；用户可用 `/owner off` 按项目关闭，并用 `/owner on` 恢复。每次使用独立临时 Thread 的 Project Owner 先把最新真人原话和解释指代所需的近期上下文放在最前，再读取项目内 `ProjectPolicy`、全部生效用户策略和项目历史；当前汇报与旧 Owner 判断只作为执行证据。项目管理历史只保存用户指令和已送达汇报，不包含隐藏推理、工具原始输出、diff、测试日志全文或 Agent 对话。Owner 作为用户的项目代理，结合用户原则、历史选择和做事风格主动判断最有价值的下一步：把握充分时直接向同一 Root Thread 发出推进、调整、补足、选路或纠偏指令；需要用户选择时在时间线最新位置给出明确意见和真实选项，不为凑数编造方案。续推已授权工作与交付下一步选项在同轮并存：Owner 的 decide 结果可携带 user_options，续推同轮仍会在时间线最新位置发布“本轮推进中的下一步选择”选项卡；用户选择 execute 才启动新方向，record_only 只记录选择；任何可操作的下一步建议必须选项化，不得只在决定文本里留“下次建议”。每轮结果审阅至少给出一条具体意见；无需继续执行时明确建议保持现状、关注点或下次最值得做的事，并设置 `continue_execution=false`。旧 Owner 决定只作技术关联，不形成父子决定链，也不继承为用户权威。决定依据、来源和模型用量折叠展示。Project Owner 与执行 Agent 完全隔离，不是 Coordinator、Reviewer 或 Tester，不读源码、不规划实现、不编码、不调度。
- 项目历史中的真人原话和选择始终是权威。`ProjectPolicy` 不复制具体指令，只保存用户明确长期表达的 `explicit` 原则，或至少两处独立真人来源支持的 `inferred` 原则。每条原则保留全部来源原文、时间和相邻上下文；`inferred` 只用于建议和低风险可撤销推进，不能冒充用户授权。单次命令、当前候选、数字、临时停止点和一次性选择不持久泛化。旧格式规则保留作审计但不进入 Root/Owner 活跃原则上下文。
- 每次 Owner 调用都把实际提供给它的公开上下文快照写入项目事件日志；每次真人提交选择都记录当时的问题、可见选项、最终选择、选择人和来源。日志不得包含隐藏推理、秘密答案或工具原始输出；历史选择只作为后续理解用户方向的带上下文证据，不自动升级为硬规则。
- turn 结束仅表示项目回到 idle，项目和 thread 仍可继续迭代。
- MAC 只负责飞书交互、项目/目录绑定、持久状态投影、费用/性能记录，以及可选外部 coding-agent CLI 桥接。
- MAC 不实现 Coordinator Agent、阶段状态机、固定 planner/coder/tester/reviewer 角色、关键词意图路由或自定义 root workflow prompt。
- 用户明确选择自动接管后，可配置一次公开的 Codex → Kimi 故障接管。宿主必须先确认旧执行及子进程停止，持久保存 provider、目标会话和本项目公开进度，再通过 outbox 告知切换。沿用 provider 原生执行循环，不静默切换、不循环换助手，不因答案发送失败重跑成功任务。

Codex 协议和行为的实现基准：

- tag：`rust-v0.144.1`
- commit：`44918ea10c0f99151c6710411b4322c2f5c96bea`
- 本地只读参考：`/private/tmp/mac-codex-ref.WvksLp`

涉及 thread、turn、steer、interrupt、dynamic tools、审批、用户提问或 multi-agent 的改动，必须先对照上述官方源码；不得凭印象自行设计相似协议。

## 设计红线

1. 不复制 Codex 已经拥有的能力。MAC 不代理或重写 Codex 的控制循环、工具循环、上下文管理和子 Agent 协作。
2. 不做关键词路由。自然语言交给模型理解；只允许显式 slash command 做项目级控制。
3. 不为单个失败案例加硬编码分支。先定位共同根因，再补覆盖同类输入的回归测试。
4. 最小可用上下文。每次执行必须获得完成任务所需的信息；Owner 明确区分当前轮和最近 5 轮，完整旧历史按需读取；旧测试日志、无关事实和传输细节不注入模型上下文。
5. Session 隔离。不同项目、聊天和 Codex thread 不共享对话内容；共享事实只能通过用户明确绑定的目录或工具读取。
6. 前后台一致且时间有序。卡片是按发送顺序排列的事件记录，不是跨时间原地改写的数据库视图；只有时间线中最新的活动卡可以原地刷新。用户消息、用户提交、Owner 决定、问题、审批或调整一旦出现在它之后，旧卡立即冻结，后续进度和终态必须在最新位置新发卡片；Provider 是否继续同一 Turn 与展示卡是否轮换相互独立。所有用户可见输出走卡片/outbox。
7. 用户交互最少但信息不减。能由 Agent 和工具获得的信息不询问用户；必须询问时优先选择题；汇报、进度、问题和结果仍需完整呈现。
8. Provider 失败原样返回 Root Agent。runtime 层不得静默切换 provider、编造恢复步骤或把失败归入固定角色流程。

## 修改权限与范围

- 未经用户确认，不修改源码、prompt、配置、环境变量，不启动/停止服务，不删除或覆盖数据。
- 用户要求实现修改后，只做任务范围内的最小改动；不得顺手加入新的抽象或流程。
- 工作树可能包含用户修改。保留无关变更，不使用 `git reset --hard`、`git checkout --` 等破坏性命令。
- 旧数据与新模型不兼容时，只有用户明确允许后才可清理。
- 推送前扫描当前 tree 和 git 历史中的密钥；含旧 secret 的本地备份分支禁止 push。

## 测试术语（T1–T9）

- T1：静态边界检查（编译、导入、架构禁止项）。
- T2：单元测试（纯函数、模型和小组件）。
- T3：组件集成测试（数据库、app-server 协议、outbox、卡片动作）。
- T4：本机 API/进程测试（真实服务、健康检查、重启恢复）。
- T5：真实飞书私聊界面黑盒。像手机用户一样在已登录飞书 UI 点击、输入和查看卡片；不得用注入接口冒充。
- T6：真实外部模型/provider 测试。
- T7：完整新项目交付黑盒。
- T8：已有目录迭代、修改和 debug 黑盒。
- T9：生成项目运行验收（真实网页/API/文件能力及稳定性）。

“测试完成”必须明确列出执行了哪些层级。脚本显示 completed、FakeRuntime 或测试注入接口都不能替代 T5。

## 每次修改后的验证

### 1. T1–T3

```bash
cd /Users/yihanwang/multi-agent-coding-system
source .worktrees/feat-core/.venv/bin/activate
python -m compileall -q src tests
pytest tests/ -q
```

### 2. 安装并重启（影响运行服务时）

```bash
source .worktrees/feat-core/.venv/bin/activate
pip install -e .
./scripts/stop_server.sh
./scripts/start_server.sh
```

- 验证/生产禁止 `uvicorn --reload`。确需开发热更新时只能监听 `src/`。
- 确认 18080 正常监听、`/admin/projects` 返回 200、飞书 WebSocket 已连接。
- `.env` 不得持久写入 `FEISHU_TEST_MODE=1`；脚本需要时仅临时 export。

### 3. T4 和自动 E2E

运行与本次改动匹配的 API/协议场景。自动场景用于快速回归，不能声明为飞书界面黑盒。

### 4. T5（用户要求或飞书交互改动时）

通过已登录飞书界面真实发送消息、点击卡片，至少验证：

- 新项目首次请求；
- 同项目多轮迭代；
- 运行中 steer；
- `request_user_input` 选择、提交、卡片回执及同 turn 续接；
- 项目切换和 session 隔离；
- 已有目录修改/debug；
- 服务重启后恢复 thread；
- 失败信息可见且没有重复卡片。

同一聊天窗口不要并发执行多个 E2E 场景。

### 5. 日志和 UI 核对

- 检查最新 `server_*.log` 的异常、重复发送、超时、卡片失败和敏感信息。
- 不能只看终态；逐轮核对 Agent 输入、关键工具事件、最终输出、数据库状态和飞书卡片是否一致。
- 若 API 额度、网络或模型端点导致失败，如实记录，不能宣称通过。

## 运行注意事项

- 服务端口：18080。
- 数据库：SQLite `mac_system.db`。
- 项目目录：`projects/`，或用户通过 `/bind` 绑定的 Git 目录。
- 服务启动：`./scripts/start_server.sh`，停止：`./scripts/stop_server.sh`。
- Agent 写项目文件时不得让 uvicorn 监听项目目录，否则会造成反复重启和 SQLite 锁冲突。
- 源码修改后必须 editable reinstall 并重启，避免旧 `.pyc`/进程造成“代码已改、运行仍旧”的假象。
- `.gitignore` 必须覆盖 `.env.bak*`、`*.db-shm`、`*.db-wal`。
