# 真实飞书 E2E 调试复盘与检查清单

> 日期：2026-07-13  
> 范围：`full_feature_flow`，重点覆盖规划产物 reject 后的多轮重写、REWORK、服务重启和真实 Claude Code stream-json Runtime。

## 1. 结论摘要

本轮发现的“reject 后卡住”不是单一状态转换错误，而是多个独立问题叠加：

1. 本地推理网关被代理环境影响，表现为 `127.0.0.1:15721` 返回 502 或连接异常。
2. Runtime 和 Claude CLI 曾同时执行工具，导致同一 Write/Edit 双执行和文件版本竞争。
3. Claude stream-json 的大型 Write/Edit 事件超过 asyncio 默认约 64 KiB 单行限制，stdout reader 静默退出，而主循环继续等待到 20 分钟超时。
4. human state 被连续 tick 两次，刚创建的 REWORK approval 被第二次 tick 过期并替换。
5. E2E 脚本把 Review 回到 Coding 后的 Coding/Testing approval 错标为 Review/Acceptance，导致向错误状态发送 reject。
6. Agent 最后一次失败后依赖“未来的下一次 tick”转 FAILED，可能留下 active、无 task、无 approval 的项目。

真实验证已经证明：提高 stream reader 上限后，需求和技术方案的 `reject → 第二轮重写 → 新 approval` 均可完成，不再因大型产物卡 20 分钟。

## 2. 环境与启动经验

### 2.1 本地网关必须绕过代理

`.env` 中应包含：

```text
NO_PROXY=127.0.0.1,localhost,::1,*.local,*.lan,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
```

仅确认 15721 端口监听不够；必须观察一次真实模型请求是否产生 token。HTTP `/` 返回 404 只能证明进程在线，不能证明推理上游可用。

### 2.2 禁止使用全目录 reload

Agent 会持续写 `projects/` 和 worktree。`uvicorn --reload` 会把正常产物写入误判为源码变更，引发：

- 服务重启；
- running task 被标记为 stale failed；
- SQLite 锁竞争；
- 飞书卡片链中断；
- 重试轮次和用户实际操作错位。

验证时使用无 reload 的单进程服务，并在代码变更后完整停止、重新安装、重新启动。

### 2.3 运行前清理并发来源

同一飞书群不要同时运行两个 scenario。启动 E2E 前检查：

- 18080 只有一个 MAC 服务；
- 数据库没有非本次预期的 running task；
- 没有遗留 `feishu_e2e.py`；
- 服务加载的是当前 worktree，而不是旧 editable install。

## 3. Runtime 根因：stream-json 单行超过 64 KiB

### 3.1 现场证据

Claude transcript 的最大单行大小：

| 场景 | 最大单行 | Runtime 结果 |
|---|---:|---|
| 第一轮需求 | 12,178 bytes | 正常收到 `result` |
| 第二轮需求 | 81,099 bytes | 旧 Runtime 等待至超时 |
| 第三轮需求 | 68,458 bytes | 旧 Runtime 等待至超时 |

reject 后的产物更详细。Claude 的 Write/Edit `tool_use` 会在一行 JSON 中携带完整文件内容，因此稳定越过 asyncio StreamReader 默认限制。

### 3.2 失败链

```text
大型 Write tool_use
  → StreamReader.readline() 越过默认限制
  → reader task 异常退出
  → 未发送 stream closed sentinel
  → Claude 实际完成并输出最终任务 JSON
  → Runtime 看不到后续事件
  → 20 分钟后 No result event received before timeout
```

Claude 本地 transcript 和 worktree 产物都证明 Agent 已经完成；问题发生在 MAC 读取 stdout 的一层。

### 3.3 修复要求

- subprocess stream limit 至少 16 MiB；
- reader 在 `finally` 中必发关闭 sentinel；
- reader 异常通过 queue 传播给主循环并立即失败；
- 回归测试必须输出超过 64 KiB 的单行 tool event，随后继续输出 result；
- 飞书进度只发送工具名、路径、内容长度，不转发完整 Write/Edit 内容。

## 4. 工具执行权必须唯一

Claude Code CLI 是 Read/Write/Edit/Bash 的唯一执行者。MAC Runtime 只负责：

- 进程生命周期；
- allowed tools 和权限策略；
- tool event 观察；
- 飞书进度；
- 最终 result 解析。

Runtime 不得再次执行 `tool_use`，也不得伪造 `tool_result`。同一工具存在两个执行者时，修改已有产物的第二轮最容易触发 `File has been modified since read`。

## 5. Human state 与 approval 幂等

### 5.1 重复 approval 的原因

当 `_advance_until_confirm()` 的起始状态已经是 REWORK 等 human state 时：

1. 第一次 tick 创建 approval A；
2. helper 发现仍是 human state，又 tick 一次；
3. 第二次 tick 创建 approval B，并把 A 过期；
4. E2E/用户点击刚收到的 A，得到 `Approval is not pending`。

### 5.2 正确规则

- 起始状态为 human confirmation/input：只 tick 一次并返回；
- 从 Agent 状态进入 human state：再 tick 一次创建卡片后返回；
- 相同 project/state 已有 pending approval：复用同一 approval，不增加 confirmation round，不生成新 ID；
- 一个项目同一时刻最多一个 pending approval。

## 6. E2E 驱动必须以真实状态为准

Review reject 回到 Coding 后，不会直接回到 Review：

```text
REVIEW reject
  → REWORK to_coding
  → CODING_CONFIRM_PENDING
  → TESTING_CONFIRM_PENDING
  → REVIEW_CONFIRM_PENDING
  → ACCEPTANCE_PENDING
```

E2E 必须逐一确认 Coding、Testing、Review，不能只等待一个 approval 就把它标记为“Review re-approval”，否则下一张 Testing 卡会被误当 Acceptance。

建议每次取得 approval 后同时通过 API/数据库断言 `approval.state`，不能只根据脚本当前位置命名。

## 7. 重试耗尽必须当场终止

最后一次允许的 Agent 执行失败时，应在当前 failure handler 中直接转 `FAILED`。禁止仅更新 retry count 后返回并等待未来 tick，否则项目可能成为：

```text
status=active
current_state=agent_execution
running task=none
pending approval=none
```

进入终态时必须同步：

- `current_state=FAILED/COMPLETED/CANCELLED`；
- `project.status=failed/completed/cancelled`。

## 8. 每轮真实 E2E 的证据清单

不能只看脚本最后一行。至少检查：

1. E2E 控制台的每个 approval ID 和阶段名；
2. `projects.current_state/status`；
3. tasks 的 state/status/round_number；
4. 每项目最多一个 pending approval；
5. Claude transcript 是否存在最终 `end_turn`；
6. Runtime 是否收到对应 `EVENT result`；
7. reject 后是否创建新 task、新 round 和新 approval；
8. 日志中是否存在：
   - `File has been modified since read`
   - `No result event received before timeout`
   - `Approval ... is not pending`
   - `Failed to handle Feishu text message`
   - `service restarted while task was running`
9. 最终状态、Git 合并结果和飞书最终卡片是否一致。

## 9. 多聊天、多项目和中途输入检查

现有设计/spec 已覆盖：

- 项目状态 project-scoped，不属于单一聊天；
- 一个聊天关联多个项目，一个项目订阅多个聊天；
- `(chat_id, open_id)` 保存独立 active project；
- 群聊不同成员的上下文互不影响；
- approval_id/project_id 的显式路由优先于 active project；
- Agent 运行期间命令优先，自由文本持久化为 pending user input；
- 输入创建时固化 project_id/source_chat_id/sender_id/observed_state；
- scope change/correction 在 Agent 返回后阻止旧结果继续推进。

主要规范入口：

- `specs/conversation_routing_spec.md`
- `specs/concurrency_and_commands.md`
- `specs/state_machine_semantics.md`
- `specs/orchestrator_spec.md`
- `specs/feishu_bridge_spec.md`

系统级需求现已集中到 `specs/product_requirements.md`，多聊天、多项目、中途命令/文本和目录绑定都有稳定 REQ/AC。后续 E2E 仍需把每个 scenario 与对应 AC 显式关联，避免只验证状态名而漏掉路由和权限语义。

## 10. 状态机 E2E 的示例需求必须保持最小

`full_feature_flow` 的目标是验证确认、reject、select_option、REWORK 和完整重放，不是评估 Agent 能否一次实现大型应用。使用含糊的“实现 Todo List CLI”会被 Requirements/Architecture 扩展为编辑、删除、搜索、统计、持久化、颜色、并发写等完整产品，导致单轮 Coder 接近场景超时，后续每次回退都重复承担无关成本。

状态机 E2E 应明确最小范围和排除项，例如只保留 `add/list`、进程内存储，并禁止编辑、删除、权限、持久化和网络功能。各轮 reject 反馈也只修正同一范围内的边界行为，不能在 Acceptance 阶段突然加入权限或数据持久化。复杂应用能力应放在独立的质量/负载 scenario 中。
