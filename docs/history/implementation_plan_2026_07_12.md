# 实施计划：E2E 分支覆盖与交互优化

> 版本：v1.0
> 创建时间：2026-07-12
> 优先级排序：P0（阻塞）→ P1（重要）→ P2（优化）

---

## P0：E2E full_feature_flow 分支覆盖

### 目标

重写 `scenario_full_feature_flow`，覆盖全部 7 个确认节点的 reject/select_option 分支。

### 覆盖清单

| 阶段 | 分支 | 驳回意见 | 验证点 |
|---|---|---|---|
| 需求确认 | reject → confirm | "需求描述过于笼统，需要补充具体的功能列表和非功能性要求" | planner 重跑后产物应体现补充内容 |
| 方案确认 | reject → confirm | "技术方案缺少备选方案对比和选型理由" | planner 重跑后方案应有选项对比 |
| Spec 确认 | reject → confirm | "Spec 未覆盖边界条件和异常场景" | 重跑后 Spec 应补充边界条件 |
| 编码确认 | reject → REWORK → to_coding | "代码缺少单元测试和错误处理" | REWORK 卡片出现，选择 to_coding |
| 测试确认 | reject → REWORK → to_testing | "测试用例未覆盖边界值和异常输入" | REWORK 卡片出现，选择 to_testing |
| Review 确认 | reject → REWORK → to_coding | "存在硬编码配置项，需要抽取为配置参数" | REWORK 卡片出现，选择 to_coding |
| 验收确认 | reject → REWORK → to_requirements | "需求分析不够全面，缺少用户权限和数据持久化的规划" | REWORK 卡片出现，选择 to_requirements，全流程重跑 |

### 实施步骤

1. **重写 `scripts/feishu_e2e.py` 的 `scenario_full_feature_flow`**
   - 创建项目 → 需求确认（reject + feedback + confirm）
   - 方案确认（select_option + replan + confirm）
   - Spec 确认（reject + confirm）
   - 编码确认（reject → REWORK → to_coding）
   - 测试确认（reject → REWORK → to_testing）
   - Review 确认（reject → REWORK → to_coding）
   - 验收确认（reject → REWORK → to_requirements → 全流程重跑）
   - 重跑完成后 → 正常 confirm 到 COMPLETED

2. **验证脚本语法**：`python -c "import py_compile; py_compile.compile(...)"`

3. **运行 E2E 验证**：
   ```bash
   cd /path/to/worktree
   source .venv/bin/activate
   set -a && source .env && set +a
   python scripts/feishu_e2e.py --scenario full_feature_flow
   ```

**预计耗时**：35-40 分钟（每阶段 1-2 分钟 × ~25 步）

## P0：修复 Reject 后卡住与重复 REWORK Approval

### 已确认根因

1. 质量确认按钮的 reject 通常没有反馈文本。现有 handler resolve reject 后进入 REWORK，却走“等待用户直接回复修改意见”的分支，没有调用推进逻辑创建 REWORK approval/card，形成 active + REWORK + no running task + no pending approval 的卡死状态。
2. reject 带反馈时，推进函数从 REWORK 开始执行：第一次 tick 已创建 REWORK approval，随后又因为检测到 human state 再 tick 一次；第二次 tick 将第一条 approval 过期并新建 ID。用户或 simulator 点击第一张卡时得到过期审批，表现为再次卡住。
3. `_handle_human_confirmation` 采用“先 expire 同状态 pending approval，再创建”的方式，不具备 ensure/reuse 幂等语义，放大了重复 tick 的影响。

### 修复要求

1. 将 `_advance_until_confirm` 改为 `ensure_waiting_boundary`：起始状态已是 human state 时只 tick 一次并返回；从 agent/system 到 human state 时再额外 tick 一次后返回。
2. human handler 改为 `ensure_pending_approval`：相同 project/state 已有 pending approval 时复用，不得 expire/recreate。
3. 质量/验收 reject 无论是否带 feedback，都必须立即进入 REWORK 并确保 REWORK 卡已发送。
4. 规划 reject 无 feedback 时不 resolve 原 approval，只打开反馈输入 UI；提交 feedback 后再原子回退。
5. handler 返回前检查 stuck invariant：active 非终态必须有 running Task、pending Approval 或 paused 状态。

### 必测场景

- 点击 CODING/TESTING/REVIEW/ACCEPTANCE 的 reject 按钮（无 feedback），只产生一张有效 REWORK 卡。
- `/reject appr_xxx feedback` 只产生一张有效 REWORK 卡。
- 点击第一张 REWORK 卡可以正常选择 to_coding/to_testing 等目标。
- 重复 tick 和重复飞书 event 不改变 approval_id，不重复发起状态转换。
- 规划 reject 取消输入后仍停在原确认态，原 approval 可继续 confirm/reject。
- 任一 handler 返回后不存在 active + 非终态 + 无 Task/Approval 的项目。

---

## P1：快捷驳回 scenario

### 目标

新增单独 scenario，验证验收卡片的快捷驳回路径（`reject_to_requirements` / `reject_to_architecture` / `reject_to_specs`）。

### 实施步骤

1. **在 `scripts/feishu_e2e.py` 新增 `scenario_acceptance_quick_reject`**
   - 创建项目 → confirm 到验收
   - 验收卡片发 `/confirm appr_xxx reject_to_requirements`
   - 验证状态直接回到 `PLANNING_REQUIREMENTS`（跳过 REWORK）
   - 可选的：再跑完剩余流程验证完整性

2. **注册到 `SCENARIOS` 字典**

---

## P1：Spec 文档更新

### 已完成

- `state_machine_semantics.md` — 新增 Reject 交互流程，更新 reject 事件描述
- `orchestrator_spec.md` — 新增 Reject 交互流程、质量确认关、幂等防护章节
- `feishu_bridge_spec.md` — 新增驳回后 interim 消息描述；新增 Agent 实时进度卡片（§7.4）
- `mvp_tasks.md` — 更新版本号 v2.0，补充 8 个确认节点的状态机

### 待完成

- AGENTS.md 纪律红线补充：reviewer 有 blocking issue 时必须 fail 的规则

---

## 已知实现缺口

### `select_option` 实现不完整

`state_machine_semantics.md` 中定义的 `select_option` 事件应携带 `decision_id` 和 `option_key`，让用户从架构方案的多个决策选项中选一个，planner 按选择重写方案。

当前实现的问题：
- 架构确认卡片的"选择其他选项"按钮只发送 `action="select_option"`，未携带 `decision_id` 和 `option_key`
- 效果等同于驳回（回到 `PLANNING_ARCHITECTURE`），用户的选择未传递给 planner
- 架构方案需要先稳定产出多个可选项（目前 planner 只输出一个推荐方案，决策在 `decisions` 数组中但卡片未渲染为可选按钮）

**修复条件**：架构阶段 planner 能产出多个可选项，且卡片能将 `decision_id` + `option_key` 渲染到按钮 value 中。

**当前 E2E 替代**：方案确认阶段使用 reject（驳回）代替 select_option，覆盖的是"重做方案"路径，与 select_option 走同一条状态机路径。

---

## P1：规划驳回意见输入卡片

### 目标

规划确认点驳回后用飞书输入卡片收集必要修改意见。质量确认/验收驳回不等待该输入卡，直接进入 REWORK。

### 设计方案

1. **在 `card.py` 新增 `build_rejection_input_card(approval_id, state, chat_id, chat_type)`**
   - 使用飞书 `tag: "input"` 元素
   - 包含文本输入框 + "提交反馈"按钮
   - 用户填写后点击提交，反馈通过卡片 action 回调提交

2. **在 `handlers.py` 修改驳回处理逻辑**
   - 规划确认点击驳回后不 resolve 原 approval，改发输入卡片
   - 输入卡片提交时，在同一事务创建 feedback packet、resolve reject、回退 Planner
   - 质量确认点击驳回时不走输入卡，调用 `ensure_waiting_boundary` 创建/复用 REWORK 卡

3. **在 `bridge.py` 新增 `send_rejection_input_card()` 方法**

**注意**：飞书输入卡片的事件回调处理与普通卡片不同，需验证 `lark-oapi` SDK 对 `tag: "input"` 的支持程度。

---

## P2：Reviewer prompt 加强

### 目标

在 `prompts/reviewer.md` 中明确 `blocking` 的判断标准，减少 LLM 把严重问题标为非阻塞的情况。

### 改动

在 prompt 中增加：

```
blocking 判断标准：
- blocking: true（必须）：安全漏洞、逻辑错误、功能缺失、与 spec/需求不一致
- blocking: false（可选建议）：代码风格、命名建议、可选的性能优化、注释完善
```

---

## P0（追加）：Agent 实时进度推送

### 目标

Agent 执行期间，将 assistant 事件的 thinking/text 和 tool_use 通过飞书卡片实时推送，让用户在群里看到 agent 的中间过程而非等待数分钟后才有结果。

### 改动范围

1. **`src/mac_system/agents/runtime.py`** — `execute()` 新增可选 `progress_callback` 参数，`_run_session` 透传，收到 assistant 事件时回调
2. **`src/mac_system/agents/orchestrator.py`** — `_execute_agent` 在启用时传入 `progress_callback`，通过 `FeishuBridge` 推送卡片
3. **`src/mac_system/config.py`** — 新增 `debug_agent_progress: bool` 配置项
4. **`specs/feishu_bridge_spec.md`** 已更新（§7.4 Agent 实时进度卡片）

### 设计约束

- **默认关闭**，通过 `DEBUG_AGENT_PROGRESS=1` 或项目配置 `debug_agent_progress: true` 启用
- 回调中不抛异常（失败只记日志，不影响 agent 执行）
- 高频 thinking 合并发送（≤2s 间隔），避免飞书限流

--- 

## 执行顺序

```
P0 E2E full_feature_flow 重写  →  P1 快捷驳回 scenario  →  P1 驳回输入卡片
  ↓                                      ↓
P1 Spec 文档更新（并行）        P2 Reviewer prompt 加强
```

---

## 新增失败教训：human state 不可重复 tick 后重建 Approval

- **表面现象**：用户已经点了 reject，项目显示 REWORK，但没有后续按钮；或者出现 REWORK 卡却点击后提示审批过期。
- **为什么 1**：无反馈 reject 进入 REWORK 后 handler 提前返回，没有创建等待边界。
- **为什么 2**：带反馈路径对 REWORK 连续 tick 两次，生成两条不同 approval。
- **为什么 3**：human handler 把每次 tick 当成“创建新审批”，而不是幂等的“确保审批存在”。
- **根本原因**：推进函数没有区分“进入 human state”和“已经位于 human state”，系统也没有强制 waiting-boundary invariant。
- **禁止的表面修复**：不能靠增加 max_ticks、sleep、重试点击或让 simulator 选择最新卡片掩盖问题。
- **根治原则**：human state tick 幂等、approval ID 稳定、每次 handler 返回都必须落在可观察等待边界。
