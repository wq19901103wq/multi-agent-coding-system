# Live Issues 调查与修复记录：API 401 + 私聊输入卡片异常

> 创建时间：2026-07-18（Asia/Shanghai）  
> 关联项目：`proj_20260718_102234_f42e`  
> 关闭时间：2026-07-19（Asia/Shanghai）  
> 本文保留现场证据、最终根因、修复与验证结果，避免后续重复排查。

---

## 1. 问题总览

| 问题 | 现象 | 影响 | 当前状态 |
|------|------|------|----------|
| API / control 端点 401 | 调用 `/projects/{id}` 等接口返回 401 | 外部脚本 / Admin 面板若漏传 token 无法使用 | 已定位：服务端配置正常，调用方需正确传 token |
| 私聊收到输入卡而非选择题 | planner 产出 `ambiguities` 后被渲染为 3 个自由文本输入框 | 用户期望点按钮，实际要打字回答 | 已修复并经真实飞书选择题/自定义答案场景验证 |
| planner 连续 401 失败 | `proj_20260718_102234_f42e` 进入 `PLANNING_REQUIREMENTS_RECOVERY_PENDING` | 项目卡住，用户已点 retry 仍失败 | 已定位并修复：用户级 Claude settings 覆盖 MAC 显式端点和凭证 |

---

## 2. 根因分析

### 2.1 API / control 401

- `src/mac_system/api/dependencies.py` 中 `require_control_api_token` 同时检查：
  - `Authorization: Bearer <CONTROL_API_TOKEN>`
  - `X-Control-Api-Token: <CONTROL_API_TOKEN>`
- `.env` 中已配置 `CONTROL_API_TOKEN`。交接文档和命令示例禁止记录真实值。
- `/admin/*` 由 `admin_routes.py` 的 `_admin_token_dependency` 保护，它只认 URL query param `token` 且只在 `ADMIN_TOKEN` 存在时校验；当前 `.env` 未配置 `ADMIN_TOKEN`，所以 `/admin/projects` 目前是开放的。
- 现场验证（对 `proj_20260718_102234_f42e`）：

  ```bash
  curl http://127.0.0.1:18080/projects/proj_20260718_102234_f42e
  # 401

  curl -H "Authorization: Bearer $CONTROL_API_TOKEN" \
       http://127.0.0.1:18080/projects/proj_20260718_102234_f42e
  # 200

  curl -H "X-Control-Api-Token: $CONTROL_API_TOKEN" \
       http://127.0.0.1:18080/projects/proj_20260718_102234_f42e
  # 200
  ```

**结论**：401 不是服务端配置问题，是调用方没传 token 或 token 不对。

### 2.2 输入卡片而非选择题

时间线（来自 `feishu_event_logs` 和 `approvals` / `questions` 表）：

1. 10:03:39 — 用户在私聊点击了 coordinator 的 clarification card，选择「完整版」。
2. 10:04 — 创建了项目 `proj_20260718_102234_f42e`。
3. 10:23:24 UTC — planner 任务 `task_20260718_102324_7106` 成功完成 `PLANNING_REQUIREMENTS`。
4. 10:27:00 UTC — Orchestrator 检测到产物 `REQUIREMENTS.json` 的 `ambiguities` 有 3 个未澄清项，强制进入 `ask_user`。
5. 生成的三个 `questions` 记录：

   | question_id | 内容摘要 | options | allow_free_text |
   |-------------|----------|---------|-----------------|
   | q_ambiguity_0 | 是否接受默认的四个技能分类？ | `[]` | `1` |
   | q_ambiguity_1 | 是否接受轻量本地持久化方案？ | `[]` | `1` |
   | q_ambiguity_2 | 项目详情是否作为简历查询子能力？ | `[]` | `1` |

6. 用户 10:28 回复「照搬 rpa 项目的 然后加上简历问答和 github query 不行吗」、10:28:55 回复「接受」、12:30:56 回复「OK」，三次都是在回答输入卡。

**代码路径**：

- `src/mac_system/agents/orchestrator.py`:`_extract_unresolved_ambiguities()` 从 `REQUIREMENTS.json` 读取 `ambiguities`。
- `src/mac_system/agents/orchestrator.py`:`_synthesize_question_batch_from_ambiguities()` 把每个 ambiguity 转成：

  ```python
  {
      "allow_free_text": True,
      "options": [],
      ...
  }
  ```

- `src/mac_system/agents/orchestrator.py`:`_send_question_batch_card()` / `src/mac_system/feishu/handlers.py`:`_send_next_free_text_card()` 最终渲染为 `build_input_card`（自由文本输入框）。

**planner prompt 已有规则但未被遵守**：

`prompts/planner_requirements.md` 第 13–14 行明确要求：

> 发现关键产品行为不明确时使用 `ask_user`，不要留给独立的“产品 Spec”阶段。  
> `ambiguities` 字段只用于已附带默认假设、非阻塞的信息性说明；任何需要用户确认/选择的模糊点必须通过 `ask_user` 解决。

但实际产物仍把需用户确认的决策项放进了 `ambiguities`。

**结论**：
- planner 没有按规范把“有默认假设但需用户确认/选择”的项走 `ask_user` + 带 `options` 的 `question_batch`。
- 系统兜底也未将这类 `ambiguities` 渲染成选择题，而是自由文本输入。

### 2.3 planner 连续 401 失败

当前项目状态：

```sql
SELECT id, current_state, status, updated_at FROM projects
WHERE id='proj_20260718_102234_f42e';

-- proj_20260718_102234_f42e | PLANNING_REQUIREMENTS_RECOVERY_PENDING | active | 2026-07-18 12:32:05
```

失败序列（DB `tasks` 表）：

| task_id | created_at (UTC) | status | 错误 |
|---------|------------------|--------|------|
| task_20260718_123056_5646 | 12:30:56 | failed | API Error: 401 |
| task_20260718_123105_4161 | 12:31:05 | failed | API Error: 401 |
| task_20260718_123112_bb54 | 12:31:12 | failed | API Error: 401 |
| task_20260718_123145_406d | 12:31:45 | failed | API Error: 401 |
| task_20260718_123153_e1a8 | 12:31:53 | failed | API Error: 401 |
| task_20260718_123159_8b58 | 12:31:59 | failed | API Error: 401 |

错误原文：

```text
Failed to authenticate. API Error: 401
The API Key appears to be invalid or may have expired.
Please verify your credentials and try again.
```

**但 token 本身测试通过**：

在同一 worktree 下，用相同的 `ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN=<redacted>`、`ANTHROPIC_MODEL=deepseek-v4-flash` 手动执行：

```bash
printf '{"type":"user","message":{"role":"user","content":"hi"}}\n' | \
  claude --output-format stream-json --input-format stream-json --verbose \
         --system-prompt-file prompts/conversation_coordinator.md \
         --model deepseek-v4-flash
```

返回 HTTP 200 并正常输出。

**最终根因**：`~/.claude/settings.json` 中配置了另一套 Kimi 端点和凭证。Claude CLI 默认加载用户级 settings 与插件，覆盖了 MAC Runtime 显式注入的 DeepSeek 环境；因此直接 HTTP 和隔离配置的 CLI 调用成功，而服务内未隔离的 CLI 调用会 401。

**修复**：`src/mac_system/agents/runtime.py` 的所有 Claude CLI 命令统一增加 `--setting-sources ""`，流式运行同时固定使用 `--bare`，使项目 Runtime 只使用 MAC 显式配置，不再读取用户级 settings 或插件。

**验证结论**：Runtime 命令构造单测通过；真实飞书项目 `proj_20260719_020155_d373` 全分支运行至 `COMPLETED`，本轮日志未出现 `API Error: 401` 或 `authentication_failed`。历史项目 `proj_20260718_102234_f42e` 也在服务重启恢复后成功完成 planner 并推进到 `ARCHITECTURE_CONFIRM_PENDING`。

---

## 3. 修复方案与实施状态

### 方案 A：修复输入卡片问题（已实施并验证）

1. **强化 planner prompt（已实施）**  
   在 `prompts/planner_requirements.md` 中增加更严厉的规则：
   - 任何带 `default_assumption` 且需要用户确认或可能让用户做选择的项，**禁止**写进 `ambiguities`。
   - 必须输出 `next_state_event: ask_user` + `question_batch`，且每个问题必须提供 `options`（至少包含“接受默认假设”和“我要修改/其他”）。
   - `ambiguities` 只能用于纯信息性说明，不得带问答句式。

2. **给系统兜底加选择题转换（已实施）**  
   在 `src/mac_system/agents/orchestrator.py`:`_synthesize_question_batch_from_ambiguities()` 中，遇到有 `default_assumption` 的歧义项时，自动生成选项：

   ```json
   {
     "options": [
       {"key": "accept_default", "label": "✅ 接受默认假设", "recommended": true},
       {"key": "modify", "label": "✏️ 我要修改"}
     ],
     "allow_free_text": true
   }
   ```

   这样即使 planner 违规，用户也能看到按钮。当前实现不会把“我要修改”误当成最终答案：提交选择后会更新原卡作为回执，再发送定向输入卡；用户补充具体要求后才恢复 Agent。

3. **重启服务**  
   因为改了 `orchestrator.py`，必须：

   ```bash
   cd /Users/yihanwang/multi-agent-coding-system
   source .worktrees/feat-core/.venv/bin/activate
   pip install -e .
   ./scripts/stop_server.sh
   ./scripts/start_server.sh
   ```

   注意：`start_server.sh` 已禁用 `--reload`。

4. **重试项目**  
   在飞书里对恢复卡点击「重试」，或调用 control API：

   ```bash
   curl -X POST \
     -H "Authorization: Bearer $CONTROL_API_TOKEN" \
     http://127.0.0.1:18080/projects/proj_20260718_102234_f42e/tick
   ```

   然后观察 `server_*.log` 是否有新的 `API Error: 401`。

### 方案 B：仅修复当前项目（禁止直接改库）

不得直接修改 `questions` / `question_answers` 绕过正常交互，这会破坏审计、幂等和 `confirmed_brief` 同步。当前项目只能通过恢复卡或带认证的 Control API 重试；若必须进行数据修复，应先实现可审计的正式恢复操作并单独获得用户授权。

---

## 4. 关键命令速查

```bash
# 进入环境
cd /Users/yihanwang/multi-agent-coding-system
source .worktrees/feat-core/.venv/bin/activate
set -a && source .env && set +a

# 验证 control token
curl -H "Authorization: Bearer $CONTROL_API_TOKEN" \
     http://127.0.0.1:18080/projects/proj_20260718_102234_f42e

# 验证 LLM 端点
curl -X POST https://api.deepseek.com/anthropic/v1/messages \
  -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"deepseek-v4-flash","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'

# 查看项目状态
sqlite3 mac_system.db \
  "SELECT id, current_state, status, updated_at FROM projects WHERE id='proj_20260718_102234_f42e';"

# 查看最近失败任务
sqlite3 mac_system.db \
  "SELECT id, agent_type, state, status, created_at FROM tasks \
   WHERE project_id='proj_20260718_102234_f42e' ORDER BY created_at DESC LIMIT 10;"

# 搜索 401 日志
grep -E "API Error: 401|authentication_failed" server_*.log
```

---

## 5. 交接检查清单

- [x] 已确认并完成「方案 A」代码修改（planner prompt + orchestrator 兜底 + 自定义答案两步交互）。
- [x] 全量单元测试通过：`283 passed`，2 个第三方弃用警告（2026-07-19）。
- [x] 执行 `pip install -e .` 并以无 `--reload` 的 uvicorn 重启验证服务。
- [x] 使用新项目 `proj_20260719_020155_d373` 复验，planner 及后续 Agent 均未再出现 401。
- [x] 确认新的澄清问题渲染为选择题；`requirement_custom_question` 同时验证“我要修改”后的定向文本补充。
- [x] `pytest tests/ -q` 最新通过基线为 `283 passed`；测试收集数量不再当作通过数量。
- [x] 已确认 `~/.claude/settings.json` 覆盖 `.env` 是历史 LLM 401 根因，并在 Runtime 层隔离用户配置。
- [x] 已更新 `CURRENT_STATE.md`，将三项 Live Issues 标记为已关闭。
