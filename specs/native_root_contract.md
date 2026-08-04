# MAC Native Root Contract

MAC 的智能执行内核直接使用官方 Codex `app-server`。当前基线为
`rust-v0.144.1`，commit `44918ea10c0f99151c6710411b4322c2f5c96bea`。

## 不由 MAC 重写的能力

- 原生 Codex thread / turn 生命周期；
- 工具循环、上下文压缩与会话续接；
- Codex 原生多 Agent 协作；
- 模型对自然语言任务的理解、规划、执行和自我验证；
- `request_user_input` 的发起时机和同轮恢复。

## MAC 只负责的适配

- 一个 MAC project 持久映射到一个 Codex thread；
- 飞书消息原样进入当前 thread；
- 将原生事件投影为持久状态、卡片和可观察进度；
- 把 `request_user_input` 投影为飞书卡片，并将答案按官方协议回传；
- 通过一个动态工具暴露可选的外部 coding-agent CLI；Codex 自己决定是否调用，MAC 不做关键词路由或固定流水线。
- workspace-write 线程通过官方 `sandbox_workspace_write.network_access` 配置继承真实 API / E2E 所需的网络能力。

## 产品边界

- 不存在 Coordinator Agent、固定 planner/coder/tester/reviewer 或阶段状态机；
- 不增加自定义 root workflow prompt；
- 不根据用户措辞、项目名、错误文案或所谓失败类型写硬编码分支；
- slash command 只处理确定性的项目控制，普通对话全部交给 Codex；
- session/project/thread 必须一一隔离，会话续接只使用该项目持久化的 native thread id。

## UI 投影规则

- 所有用户可见输出都使用飞书卡片；
- 卡片状态必须来自同一份持久后台状态；
- 原生高频 delta 只用于当前进程内流式展示，不逐条持久化；
- 持久化稳定事件边界，例如 turn、tool call、command/file change 完成、问题、子 Agent 状态和最终结果；
- UI 投递失败不能伪造用户答案，也不能改变原生 Codex turn 的业务语义。
