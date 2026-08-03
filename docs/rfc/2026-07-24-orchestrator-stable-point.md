# RFC: Orchestrator 稳定点与反应触发收敛

> 状态：Draft
> 日期：2026-07-24
> 关联：`memory/architecture-bugs-from-private-chat.md` Bug #1 #2 #3

## 1. 背景

私聊实测 proj_20260723_004806_1358 暴露 31 分钟静默期。根因不是单点 bug，而是 orchestrator 把两个决策权外溢给了调用方：

1. **稳定点定义**：`tick()` 不定义"什么时候算停"，由调用方自己循环判断。
2. **反应触发**：`transition_state` 后不统一触发反应，由各调用方手动补。

直接证据：

- `feishu/handlers.py:3471-3522` 的 `_advance_until_confirm` 在飞书层做状态机稳定点判断。注释 L3480-3482 直说 "Human_confirmation states need an explicit tick to create the approval card" -- 这是 orchestrator 不保证卡片发送的 workaround。
- `orchestrator.py:1464-1465` 在 `_handle_agent_output` 里手动判断 `human_input` 并调 `_handle_human_input`，但漏了 `human_confirmation`：
  ```python
  if state_machine.state_type(new_state) == "human_input":
      await self._handle_human_input(project, state_machine, auto_resolve=False)
  ```
  这是 Bug #1 #2 的现场。
- `feishu/handlers.py:1615-1618` coordinator rollback 只调单次 `tick`，agent 完成转 `human_confirmation` 时卡片永远不发。

"发卡片"是表相。真正要收拢的是上述两个决策权。

## 2. 目标与非目标

### 2.1 目标

- `tick()` 内部循环到稳定点后返回；调用方不再选"用哪种 tick"
- `transition_state` 后由 orchestrator 统一根据新状态类型触发反应
- `_advance_until_confirm` 从 feishu 层消失

### 2.2 非目标

- 不改副作用实现层（feishu bridge / notification_service 仍负责具体卡片格式）
- 不引入事件总线（见 §3.3）
- 不在本 RFC 处理 Bug #4 #5（agent 失败通知、长时进度卡）-- 单独 RFC

## 3. 设计

### 3.1 `tick()` 新契约

```python
async def tick(self, project_id: str) -> None:
    """推进项目直到到达稳定点。

    稳定点（停止循环）：
    - human_confirmation: 已创建/复用 Approval 并发送卡片
    - human_input: 已创建/复用 input Approval 并发送卡片
    - terminal: 终态
    - idle: 空闲态

    非稳定点（继续循环）：
    - agent_execution: agent 完成后状态通常转变
    - system_action: 系统动作完成后状态通常转变

    循环上限：max_ticks=10（沿用 _advance_until_confirm 现值）
    退出条件：状态未变化 / paused / cancelled / 锁竞争失败
    """
```

实现要点：

- `_tick_unsafe` 改为循环：dispatch -> 检查新状态 -> 非稳定点继续 dispatch -> 稳定点返回
- 状态未变化时退出（沿用 `_advance_until_confirm` 的保护）
- paused/cancelled 立即返回（保留 L165-166 行为）
- 锁仍然在外层获取一次，循环在锁内进行

#### 3.1.1 Loop 入口已在 human 态的行为

`tick()` 被调用时项目可能已经处于 `human_confirmation` / `human_input`（服务重启恢复、API 触发、卡片重发）。此时：

- 第一轮 dispatch 调用 `_handle_human_confirmation` / `_handle_human_input`
- 这两个方法本身幂等（orchestrator.py:2108 注释 "Human-state ticks are idempotent. Reuse an existing pending approval"），会创建或复用 Approval 并发卡
- 状态通常不变 -> "状态未变化退出" 规则触发，loop 退出

特殊情况：night/auto 模式下 `_handle_human_confirmation` 会自动 confirm 并 transition 到下一 stage。此时状态变化，loop 继续推进到下一个稳定点。这符合期望。

无需为 loop 入口在 human 态做特殊分支，"状态未变化退出" 规则已覆盖。

### 3.2 transition_state post-hook 反应类型

新增 `_trigger_post_transition_reaction(project, new_state)`，在 `_handle_agent_output` 完成 transition 后调用：

| 新状态类型 | 反应 | 复用现有方法 |
|---|---|---|
| `human_confirmation` | 创建/复用 Approval + 发卡 | `_handle_human_confirmation` |
| `human_input` | 创建/复用 input Approval + 发卡 | `_handle_human_input` |
| `terminal` | 发终态通知 | `_notify_state_transition` |
| `agent_execution` (重试) | 仅日志，由 tick 循环继续推进 | - |

`_handle_agent_output:1464-1465` 的手动 `human_input` 判断删除，由 post-hook 接管。这同时修掉 Bug #2（human_confirmation 漏处理）。

### 3.3 feishu bridge 订阅机制

审阅 bot 提出的开放问题：feishu bridge 是 orchestrator 的依赖还是外部消费者？

**结论：直接调用，不引入事件总线。**

理由：

- `AgentOrchestrator.__init__` 已接收 `feishu_bridge` 为可选依赖（orchestrator.py:114-121）
- `cancel_project` 已直接调 `feishu_bridge.send_text`（L154-158）
- 当前只有一个消费者，事件总线的多消费者价值未体现
- 引入事件总线会新增一层间接性，与现有代码风格不一致

orchestrator 决定"何时发什么类型的卡"，feishu bridge / notification_service 决定"卡片长什么样"。这个边界不变。

### 3.4 `_advance_until_confirm` 迁移

迁移后：

- `_advance_until_confirm` 从 `feishu/handlers.py` 删除
- 所有调用方改为 `await ctx.orchestrator.tick(project.id)`
- 调用方不再需要选"用哪种 tick"

需迁移的调用点（实施时 grep `_advance_until_confirm` 确认全量）：

- `feishu/handlers.py:1618` - coordinator rollback 路径（Bug #1 修复点）
- `feishu/handlers.py:2629` - reject 无 feedback 路径（Bug #3 修复点）
- 其他 `_advance_until_confirm` 调用点

### 3.5 API 调用方兼容性

`tick()` 改为循环后，单次调用可能从毫秒级变成分钟级（多 stage agent 串联）。需审计所有非 feishu 入口的 `tick()` 调用方。

调用点清单（grep `orchestrator\.tick\(` 确认）：

| 文件 | 行号 | 上下文 | 兼容性 |
|---|---|---|---|
| `api/main.py` | L70 | 项目创建后启动 | 兼容（期望推进到稳定点） |
| `api/main.py` | L236, L293, L472 | decision/command 后推进 | 兼容（期望推进到稳定点） |
| `api/main.py` | L345, L351, L408, L414 | message/feedback 处理后推进 | 兼容（期望推进到稳定点） |
| `api/main.py` | L829, L854 | rollback 后推进 | 兼容（期望推进到稳定点） |
| `api/admin_routes.py` | L540 | admin 手动 tick | 兼容（admin 期望一次推进到稳定点） |
| `__main__.py` | L117, L194 | CLI 入口 | 兼容 |

潜在问题：

- 这些调用方都在 async handler 内 `await orchestrator.tick(...)`，HTTP 请求会阻塞到 tick 返回。如果 tick 耗时几分钟，HTTP 超时可能触发。
- 现有 `_advance_until_confirm` 调用方已经是循环行为，feishu 路径已适应长耗时。
- API 调用方目前依赖单次 dispatch，但实际语义期望就是"推进到稳定点"，新行为更符合语义。

**实施时动作**：

1. 检查 `api/main.py` 各调用方的 HTTP 超时配置，确认能容忍分钟级耗时
2. 若有超时风险，将长耗时调用改为 background task（FastAPI `BackgroundTasks`），返回 202 Accepted
3. 文档化 `tick()` 新语义：返回时项目一定在稳定点

## 4. 实施步骤

1. **orchestrator.py**: `tick()` / `_tick_unsafe` 改为内部循环到稳定点
2. **orchestrator.py**: 新增 `_trigger_post_transition_reaction(project, new_state)` 方法
3. **orchestrator.py**: `_handle_agent_output` 删除 L1464-1465 手动判断，改调 post-hook
4. **feishu/handlers.py**: grep 找全量 `_advance_until_confirm` 调用点，全部改为 `tick()`
5. **feishu/handlers.py**: 删除 `_advance_until_confirm` 函数定义
6. **api/main.py + admin_routes.py + __main__.py**: 审计所有 `tick()` 调用方的 HTTP 超时配置；必要时改为 background task
7. **测试**: 现有 `_advance_until_confirm` 的测试用例迁移为 `tick()` 循环语义测试
8. **E2E**: 验证 coordinator rollback -> agent 完成 -> 卡片自动发送

## 5. 风险与缓解

| 风险 | 缓解 |
|---|---|
| tick 循环死锁（agent 持续失败重试） | max_ticks 上限 + 状态未变化退出 |
| 现有调用方依赖单次 tick 行为 | 实际调用方都期望"推进到稳定点"，迁移后更符合预期；少数依赖单次的需审查 |
| post-hook 抛异常导致 tick 中断 | post-hook 内部 try/except，记日志不抛 |
| 测试覆盖不足 | 保留 `_advance_until_confirm` 的所有现有测试，改为测 `tick()` |
| API HTTP 超时（tick 耗时从毫秒变分钟） | §3.5 审计超时配置，必要时改 background task |
| 锁持有时间变长 | asyncio 语义保证：agent 执行期间的 `await` 释放事件循环控制权，同 event loop 内其他 coroutine 不被阻塞。agent 子进程执行不占 Python GIL。无需额外处理 |

## 6. 验收标准

- AC-1: `tick()` 内部循环到 `human_confirmation`/`human_input`/`terminal`/`idle` 后返回
- AC-2: `_handle_agent_output` 不再手动判断 state_type，由 post-hook 统一处理
- AC-3: `_advance_until_confirm` 从 `feishu/handlers.py` 删除
- AC-4: coordinator rollback 路径调 `tick()` 后，agent 完成转 `human_confirmation` 时卡片自动发送（Bug #1）
- AC-5: reject 无 feedback 路径调 `tick()` 后，项目不再卡死（Bug #3）
- AC-6: 非 feishu 入口（API / admin / 恢复路径）调 `tick()` 行为与 feishu 入口一致
- AC-7: 现有 `_advance_until_confirm` 测试用例全部迁移为 `tick()` 测试并通过

## 7. 未解决问题

- Bug #4（agent 失败无通知）和 Bug #5（长时 agent 无进度卡）的卡片触发契约未在本 RFC 覆盖。建议作为后续 RFC：失败反应类型 `reaction_agent_failure`、进度反应类型 `reaction_agent_progress` 是否由 post-hook 统一，还是由 runtime callback 路径独立处理。
- Bug #6（runtime log 双行）是纯实施 bug，与本 RFC 无关，单独修。
