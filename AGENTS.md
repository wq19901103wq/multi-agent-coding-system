# AGENTS.md — MAC System 开发纪律

## 项目当前状态（2026-07-11）

### 已完成

- **状态机重构（v1.1）**：在 `feature` / `bug_fix` / `documentation` 三条状态机中增加：
  - 规划阶段输入态：`REQUIREMENT_INPUT_PENDING` / `ARCHITECTURE_INPUT_PENDING` / `SPEC_INPUT_PENDING`（及 bug_fix / documentation 对应输入态）
  - 质量确认关：`CODING_CONFIRM_PENDING` / `TESTING_CONFIRM_PENDING` / `REVIEW_CONFIRM_PENDING`
  - 统一的 `REWORK` 状态，支持回退到任意上游阶段（含 `to_testing`）
  - 质量失败统一走 `fail` -> `REWORK`，只有明确 `pass` 才进入确认关
- **汇报卡片与确认卡片分离**：Agent 执行进入 `human_confirmation` 后，必须先发送汇报卡片（`send_content_card`）展示本轮输出，再发送带操作按钮的确认卡片（`send_confirmation_card`）。
- **Night / Auto 模式质量关自动通过**：同一 tick 内自动 confirm，不阻塞流程；但仅当 Agent 明确判定 pass 时才生效。
- **REWORK 状态落地**：人工选择回退目标（需求 / 架构 / Spec / 编码 / 测试）。
- **结构化反馈模型**：新增 `FeedbackPacket` 与 `PendingUserInput`，替代脆弱的 `latest_rejection_feedback` 字符串。
- **Orchestrator 核心改造**：
  - Tester / Reviewer 判定失败时创建 `FeedbackPacket` 并进入 `REWORK`
  - 质量关通过时按 `verified_feedback_ids` 关闭历史反馈
  - Agent 运行期间收到的自由文本/命令被消费为 `PendingUserInput`，scope_change/correction 会强制质量关进入 `REWORK`
  - 每轮任务记录 `round_number`，旧 approval 在状态重新进入时标记 `expired`
- **单元测试基线**：`48 passed`（新增 4 个针对 FeedbackPacket / PendingUserInput / round_number 的用例）。
- **真实飞书端到端**：`full_feature_flow` 已干净跑通到 `COMPLETED`：
  - 经历了 `REQUIREMENT_CONFIRM_PENDING` → `ARCHITECTURE_CONFIRM_PENDING` → `SPEC_CONFIRM_PENDING` → `CODING_CONFIRM_PENDING` → `TESTING_CONFIRM_PENDING` → `REVIEW_CONFIRM_PENDING` → `ACCEPTANCE_PENDING` → `COMPLETED`。
  - `uvicorn.log` 中无 `Failed to handle Feishu text message`、`Failed to send summary content card` 或 `NoneType.*int` 错误。
  - 注意：**同一聊天窗口不要并发跑多个 e2e scenario**，否则 simulator 会把不同项目的 approval 卡片串扰。

### 已踩坑 & 必须规避

1. **汇报卡片被吞事件**
   - 根因：`_send_confirmation_cards` 把 `page` / `total` 传成 `None`，`build_content_card` 里 `total <= 1` 比较时报 `TypeError`，异常被吞后只剩确认卡片。
   - 结论：卡片参数必须传有效值；任何“显示优化”应在 builder 内部处理，不要传 `None`。

2. **uvicorn `--reload` 导致灾难**
   - Agent 会在 `projects/` 下大量写文件，`--reload` 监听到后会反复重启服务，导致 SQLite `database locked`、tick 中断、飞书消息处理异常。
   - 结论：验证/生产启动**禁止**用 `--reload`。如本地开发确实需要热重载，必须限制 `--reload-dir src/`。

3. **Editable install 缓存**
   - 只改源码不 `pip install -e .` 并重启，运行的是旧 `.pyc`/缓存代码。
   - 结论：每次代码修改后必须 reinstall 并重启 uvicorn。

4. **只看 e2e 脚本 `COMPLETED` 不够**
   - 之前 full flow 到了 `COMPLETED`，但汇报卡片实际被吞。
   - 结论：改完卡片/飞书交互后，必须检查 `server.log` 中是否存在 `Failed to send summary content card` 或 `NoneType` 警告，并在飞书侧确认两种卡片都收到。

5. **Prompt 里不要写相对 schema 路径**
   - `planner_*.md` 原提示把 schema 路径写成 `{{workspace_path}}/specs/schemas/xxx.json`，但项目 workspace 下并没有这个目录，导致 Agent 找 schema 耗时甚至失败/重试。
   - 结论：prompt 中 schema 路径统一用渲染器注入的 `{{output_schema_path}}`，它指向仓库根目录的绝对路径。

---

## 每次修改后的验证流程

### 重要定义

> **「测试」** 指在**真实飞书群**中运行的完整端到端测试，必须覆盖所有流程分支（confirm / reject / select_option / rework），且必须在飞书群中可观测到完整交互过程。  
> FakeRuntime 单元测试和 `test_full_flow.py` 仅作为快速辅助验证，**不能替代飞书群 E2E 测试**。

验证流程分两个阶段，缺一不可：

1. **单元测试全绿**

   ```bash
   cd /Users/yihanwang/multi-agent-coding-system/.worktrees/feat-core
   source .venv/bin/activate
   pytest tests/ -q
   ```

   当前基线：**133 passed**（含 full_flow 分支覆盖测试）。

2. **真实飞书端到端验证（必须）**

   ```bash
   source .venv/bin/activate
   set -a && source .env && set +a
   python scripts/feishu_e2e.py --scenario full_feature_flow
   ```

   必须等待 scenario 输出 `reached COMPLETED` 才算通过，且**必须在飞书群中亲眼验证完整交互过程**（卡片是否正常渲染、按钮是否可点击、状态流转是否正确）。

   **分支覆盖要求**：
   - `full_feature_flow` 场景必须覆盖每个确认节点的 **confirm** 路径
   - 后续需逐步加入 **reject**（驳回回退）、**select_option**（方案选项选择）、**rework_to 各阶段**（回退到需求/架构/Spec/编码/测试）分支
   - 若 `full_feature_flow` 尚未覆盖某个分支，应**单独编写对应 scenario** 并纳入 `--all` 执行

   **外部依赖处理**：
   - 若因 API 额度、网络、模型端点等外部原因失败，需如实记录失败原因并告知用户，不得隐瞒或宣称”已完成”。
   - 若修改的是状态机、卡片交互或飞书命令处理，`full_feature_flow` 必须能覆盖新增的质量确认关卡（`CODING_CONFIRM_PENDING` / `TESTING_CONFIRM_PENDING` / `REVIEW_CONFIRM_PENDING`）。
   - 修改 `scripts/feishu_e2e.py` 本身时，应确保 `full_feature_flow` 随新流程同步更新。
   - **额外检查**：跑完后搜索 `server.log`，确认没有 `Failed to send summary content card` 或 `NoneType.*int` 警告。

3. **服务重启确认**

   若修改影响运行中的 API / Feishu WebSocket 服务，验证前必须重启服务并确认：

   - 端口 `18080` 正常监听；
   - `/admin/projects` 返回 200；
   - Feishu WebSocket 已连接；
   - 未使用 `--reload` 启动。

---

## 关键配置

| 项 | 值 |
|---|---|
| Python | 3.10 |
| 虚拟环境 | `.venv/`（位于项目根目录） |
| 服务端口 | `18080`（`.env` 中 `MCS_PORT=18080`） |
| 框架 | FastAPI + Uvicorn |
| 数据库 | SQLite `mac_system.db` |
| 项目目录 | `projects/` |
| 飞书接入 | WebSocket 长连接（`FEISHU_TRANSPORT=websocket`） |
| 飞书凭证 | `.env` 中真实 App ID / Secret |
| Admin 面板 | `http://127.0.0.1:18080/admin/projects` |
| LLM 运行时 | Claude Code via stream-json；Kimi endpoint via local proxy |

---

## 常用命令

### 1. 启动服务（验证/生产用，**不带 reload**）

```bash
cd /Users/yihanwang/multi-agent-coding-system/.worktrees/feat-core
source .venv/bin/activate
set -a && source .env && set +a
pip install -e .
nohup uvicorn mac_system.api.main:app --host 0.0.0.0 --port 18080 > server.log 2>&1 &
echo $! > uvicorn.pid
```

启动后检查：

```bash
cat uvicorn.pid
lsof -Pi :18080 -sTCP:LISTEN
tail -20 server.log
```

### 2. 停止服务

```bash
kill "$(cat uvicorn.pid)"
```

### 3. 单元测试

```bash
pytest tests/test_feishu.py tests/test_api.py tests/test_orchestrator.py -q
```

### 4. 真实飞书 full flow

```bash
set -a && source .env && set +a
python scripts/feishu_e2e.py --scenario full_feature_flow
```

### 5. 检查汇报卡片是否发送成功

```bash
grep -E "Failed to send summary content card|NoneType.*int" server.log
```

无输出为正常。

---

## 已知待修复容错问题（2026-07-12）

### 1. 服务重启中断运行中任务
重启时 `status == "running"` 的 task 标记为 failed 后不会自动恢复。需要在服务启动时扫描并重试。

### 2. `fail_max_retries` 状态机事件缺失
`_execute_agent` 调用 `transition_state(id, "fail_max_retries")`，但所有状态机 JSON 均未定义此事件。

### 3. linter 文件写入冲突
Agent 写入 `.mac/` 文件后 linter 自动修改，导致下次写前校验失败。需在 worktree 层面跳过 linter。

### 4. 自由文本被卡住项目劫持
active/failed 项目会拦截所有自由文本消息为 pending input，用户无法通过自由文本创建新项目。

详细分析见 memory: `known-blockers.md`。

## 纪律红线

- 未经用户逐条确认，不得修改源码、prompt、配置、环境变量，不得启动/停止服务，不得删除/覆盖数据。
- 只做与当前任务相关的最小修改，禁止“顺手重构”。
- 改完后必须自己验证，不得把验证责任推给用户。
- 遇到问题先定位根因，禁止只藏表面现象。
- **Reviewer 规则**：Reviewer Agent 发现 blocking issue（安全漏洞、逻辑错误、功能缺失、与 spec/需求不一致）时，必须设 `blocking: true` 且 `result: "fail"`、`next_state_event: "fail"`。非阻塞建议（代码风格、命名、性能优化）可设 `blocking: false` 不阻塞流程。
