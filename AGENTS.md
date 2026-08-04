# AGENTS.md — MAC 开发纪律

## 当前架构（2026-08-04）

MAC 是 Codex 的移动端项目宿主，不是另一套 Agent 工作流框架。

- 一个 MAC 项目绑定一个持久 Codex root thread。
- 普通消息原样进入该 thread；运行中追加消息使用 Codex 原生 `turn/steer`。
- Root 从首轮起拥有 Codex 正常 coding tools、skills、上下文压缩和原生 multi-agent v2。
- 用户问题使用 app-server 原生 `item/tool/requestUserInput`；MAC 只把请求映射成飞书卡片并把答案送回同一 turn。
- turn 结束仅表示项目回到 idle，项目和 thread 仍可继续迭代。
- MAC 只负责飞书交互、项目/目录绑定、持久状态投影、费用/性能记录，以及可选外部 coding-agent CLI 桥接。
- MAC 不实现 Coordinator Agent、阶段状态机、固定 planner/coder/tester/reviewer 角色、关键词意图路由、自动 provider fallback 或自定义 root workflow prompt。

Codex 协议和行为的实现基准：

- tag：`rust-v0.144.1`
- commit：`44918ea10c0f99151c6710411b4322c2f5c96bea`
- 本地只读参考：`/private/tmp/mac-codex-ref.WvksLp`

涉及 thread、turn、steer、interrupt、dynamic tools、审批、用户提问或 multi-agent 的改动，必须先对照上述官方源码；不得凭印象自行设计相似协议。

## 设计红线

1. 不复制 Codex 已经拥有的能力。MAC 不代理或重写 Codex 的控制循环、工具循环、上下文管理和子 Agent 协作。
2. 不做关键词路由。自然语言交给模型理解；只允许显式 slash command 做项目级控制。
3. 不为单个失败案例加硬编码分支。先定位共同根因，再补覆盖同类输入的回归测试。
4. 最小可用上下文。每次执行必须获得完成任务所需的信息；旧测试日志、无关事实和传输细节不注入模型上下文。
5. Session 隔离。不同项目、聊天和 Codex thread 不共享对话内容；共享事实只能通过用户明确绑定的目录或工具读取。
6. 前后台一致。卡片显示的状态、已选答案和后台持久状态必须一致；所有用户可见输出走卡片/outbox。
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
