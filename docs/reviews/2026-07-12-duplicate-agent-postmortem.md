# 2026-07-12 故障复盘：重复 Agent 进程导致项目卡死

## 时间线

| 时间 (CST) | 事件 |
|---|---|
| 07/12 11:04 | 用户发送 `/create 帮我做一个个人网站`（项目 `proj_20260712_052743_0c64`） |
| 11:04 | 因用户有 active 旧项目 `proj_20260710_053838_085e`，消息被劫持为 pending input |
| ~12:00 | 用户取消旧项目后重新 `/create`，项目创建成功 |
| 12:53 | 切换到 DeepSeek provider，服务重启（中断了正在运行的 architecture task） |
| 13:34 | 修复卡片格式后再次重启（中断了重试的 architecture task） |
| 13:50 | 手动 tick 恢复 → 因 retry_counts 未清空，触发 `fail_max_retries` 报错 |
| 13:50+ | 清空 retry_counts 后手动 tick → claude 进程启动 |
| 13:54 | 用户不耐烦再次触发 tick → **第二个 claude 进程启动** |
| ~14:00 | 两个 claude 进程死锁（CPU 0.1%），项目卡死 |
| 14:34 | 杀掉两个进程，清理 worktree，单个 claude 重启架构 task |
| ~14:35 | 再次重复——第二次 tick 又启动了一个进程 |
| 14:39 | 确认根因：`_execute_agent` 无幂等检查 |

## 根因

### 直接原因
`_execute_agent` 不检查该项目是否已有 `status == "running"` 的 task。每次 `tick()` 调用都直接创建新 task 并启动新 claude 进程。多个 claude 进程操作同一 worktree，文件锁互相争抢，最终全部死锁。

### 深层原因 1：test_tick 是同步阻塞的
`/test/tick` 端点直接 `await orchestrator.tick(project_id)`，而 `_execute_agent` 内部的 `await self.runtime.execute(...)` 会阻塞到 LLM 返回（几十秒到几分钟）。用户等不到响应，以为没生效，再次触发，重复进程就此产生。

### 深层原因 2：设计-实现缺口
`specs/concurrency_and_commands.md` 写了幂等规则（同一项目不能同时占两个槽位、重复 tick 复用 approval 等），但代码从未实现。详见 `known-blockers.md`。

### 深层原因 3：FakeRuntime 掩盖了并发问题
`tests/test_full_flow.py` 和单元测试全部用 `FakeRuntime`（毫秒级返回、内存操作），永远不会触发进程冲突。真实 `ClaudeCodeRuntime` 的分钟级耗时才是并发触发的必要条件。

## 修复

1. **`_execute_agent` 增加幂等检查**：在创建新 task 前查询是否有 `status == "running"` 的同状态 task，有则跳过。
2. **已记录到 `known-blockers.md`**：四个待修复的容错问题，优先级：linter 文件冲突 > 服务重启恢复 > fail_max_retries > 自由文本劫持。

## 待办

- [ ] `concurrency_and_commands.md` 中的全局执行槽位池仍需实现（`MAX_CONCURRENT_AGENT_EXECUTIONS`）
- [ ] `test_tick` 改为 `asyncio.create_task` 异步返回，避免 HTTP 阻塞
- [ ] 增加集成测试：并发两次 tick 验证不会产生重复进程
- [ ] `_handle_human_confirmation` 同样需要幂等检查

---

## 后续调试记录（2026-07-12 晚）

### 未解问题：架构重跑卡住

需求阶段的 reject 重跑能正常工作，但架构阶段 reject 后的重跑在 `--replay-user-messages` 模式下会卡住。

**现象**：
- 第一次架构跑成功（~2.5 min）
- 驳回后重跑：agent 完成所有读取和一次 Write 调用，DeepSeek 最后一次响应被截断（`stop_reason: null`），CLI 不发 `result` 事件就退出
- runtime 循环在 `stdout_queue.get()` 死等（无超时）

**已实施的修复**：
- stream-closed sentinel：_read_stream 在 stdout 关闭时放标记到队列，主循环收到后退出（不依赖 result 事件）
- SQLite WAL 模式：防止长事务导致 `database is locked`

**修复后效果**：
- CLI 退出后 runtime 能正常返回，task 标记为 failed
- `_execute_agent` 重试逻辑（最多 3 次）会重跑，不会死等 20 分钟
- 但重试仍然可能遇到同样的问题，不是根因修复

### 踩坑记录

| # | 尝试的修复 | 后果 | 教训 |
|---|---|---|---|
| 1 | 去掉 `--replay-user-messages` | CLI 处理一个 turn 就退出，全部 E2E 挂 | stream-json 模式下此 flag 是必需的 |
| 2 | 加 `proc.poll()` | `asyncio.Process` 无此方法，全部 task 崩 | 改异步代码后必须跑 runtime 测试 |
| 3 | 加 120s per-response 超时 | 掩盖了真正的等待问题，DeepSeek 可能在正常生成 | 不要用超时当修复 |
| 4 | text-only 事件发"请继续" nudging | 产生大量无效消息，使对话混乱 | 不了解协议细节不要乱加交互 |
| 5 | 改完代码不验证进程参数 | 旧服务器进程一直存活，所有"修复"无效 | 重启后检查 claude 进程 args |
| 6 | 多个 E2E 实例串扰 | 新旧 E2E 同时发消息，approval 被抢 confirm | 跑前 `pkill -f feishu_e2e` |

### 仍需排查的问题

1. ~~架构重跑卡住~~ → 已定位（见下方「Codex 根因分析 2026-07-13」）
2. **`runtime.py` 事件级日志已加**（每个 event 的 type、tool name、elapsed time），下次卡住时可直接从 server log 看到每个事件的耗时和序列。

---

## Codex 根因分析（2026-07-13）

详见 `docs/reviews/2026-07-13-real-feishu-e2e-debugging-lessons.md`

### 核心 Bug：asyncio.StreamReader 64 KiB 行限制

| 场景 | 最大单行 | Runtime 结果 |
|---|---|---|
| 第一轮需求 | 12,178 bytes | ✅ 正常收到 result |
| 第二轮需求 | 81,099 bytes | ❌ 旧 Runtime 等待至超时 |
| 第三轮需求 | 68,458 bytes | ❌ 旧 Runtime 等待至超时 |

reject 后的产物更详细，Claude 的 Write tool_use 在一行 JSON 中携带完整文件内容，超过 `StreamReader.readline()` 默认 64 KiB 限制。

### 失败链

```
大型 Write tool_use
  → StreamReader.readline() 越过 64 KiB 默认限制
  → reader task 异常退出
  → 未发送 stream closed sentinel
  → Claude 实际完成并输出最终任务 JSON
  → Runtime 看不到后续事件（worktree 产物已存在）
  → 20 分钟后 No result event received before timeout
```

关键证据：worktree 中 ARCHITECTURE.json 已正确写入，证明 agent 早就完成了工作。问题出在 MAC 读取 stdout 的一层，不是 LLM 执行层。

### 其他发现

- **代理环境**：`NO_PROXY=127.0.0.1,localhost` 未设置，本地网关请求被系统代理劫持
- **reject 无 feedback 卡死**：质量确认 reject 走了"等待用户输入"分支，进入 REWORK 后既没创建 REWORK 卡片也没启动新 task
- **approval 幂等**：`_advance_until_confirm` 对 human state 连续 tick 两次，刚创建的 approval 被第二次 tick 过期又重建
- **E2E 状态错标**：Review 回到 Coding 后，E2E 把 Coding approval 错标为 Review/Acceptance

### 教训总结（个人复盘）

1. **在错误层面找原因** — 一直在 handler/config 层打转，从没想到是 asyncio 底层 IO 的 64 KiB 限制。问题出在 `StreamReader.readline()`，离调试范围差三个抽象层。
2. **没验证 agent 是否完成** — 从没去 worktree 目录看产物文件是否存在。Codex 查了发现文件已写好，当场定位到"问题不在 agent 执行，在 MAC 读取"。
3. **workaround 掩盖问题** — 加了 120s 超时、流关闭 sentinel、nudging，让 runtime 能"恢复"了，但掩盖了 reader 挂了的真正症状。
4. **知识盲区** — 不知道 `StreamReader.readline()` 默认限制 64 KiB，没想到 tool_use 单行 JSON 能到 80+ KiB。
5. **数据没看全** — 盯着 error_message 事件流反复分析，从没去 worktree 看一眼。

**教训：出问题先检查最底层 IO 和产物完整性，不要在上层逻辑里瞎猜。**
