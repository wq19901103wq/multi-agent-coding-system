# Prompt 模板规范

> 版本：v1.0  
> 用途：定义 Orchestrator 如何组合 system prompt、skill prompt、user prompt，并生成喂给 `claude -p` 的最终文本。

## Prompt 组成

最终传给 `claude -p` 的 prompt 由三部分按顺序拼接：

```
[system_prompt_file 内容]

---

[skill prompt 片段（可选）]

---

[user prompt 模板渲染结果]
```

实际调用方式：

```bash
claude -p "{user_prompt}" \
  --system-prompt-file {system_prompt_file} \
  --output-format json \
  --json-schema {output_schema} \
  --allowedTools "{allowed_tools}" \
  --add-dir {readonly_dir} ...
```

> 说明：`--system-prompt-file` 只接收一个文件，因此 skills 需要在 Orchestrator 渲染 system prompt 时先合并成一个临时文件。

## System Prompt 渲染

### 基础 system prompt

来自 `agent_config.json` 中 `prompt_file` 指定的文件，如 `prompts/coder.md`。

### Skills 注入

`agent_config.json` 中 `skills` 列表指定要注入的技能。每个 skill 是项目 `skills/` 或 `.kimi/skills/` 目录下的 `SKILL.md` 文件。

注入规则：

1. 读取 skill 文件内容
2. 若 skill 文件是标准 Markdown，取 `# Skill Content` 之后的内容；否则取全文
3. 将 skill 内容作为 **system prompt 追加片段** 拼在基础 system prompt 之后
4. 生成临时文件：`/tmp/mac_system/{project_id}/{task_id}_system_prompt.md`

示例：

```markdown
# Coder Agent

你是一个专业的 Python 开发工程师...

---

## 技能：中文代码审查

（来自 chinese-code-review skill 的内容，仅 reviewer 注入）
```

## User Prompt 模板变量

User prompt 文件（如 `prompts/coder.md` 或 `prompts/planner_requirements.md`）中可使用以下变量：

| 变量 | 类型 | 说明 |
|------|------|------|
| `{{project_id}}` | string | 项目 ID |
| `{{task_id}}` | string | 当前 Task ID |
| `{{agent_type}}` | string | Agent 类型 |
| `{{state}}` | string | 当前状态机状态 |
| `{{task_type}}` | string | 项目任务类型 |
| `{{description}}` | string | 用户原始需求描述 |
| `{{state_json_path}}` | string | worktree 中 `STATE.json` 路径 |
| `{{task_json_path}}` | string | worktree 中 `TASK.json` 路径 |
| `{{output_schema_path}}` | string | 期望输出 JSON schema 路径 |
| `{{worktree_path}}` | string | 当前 worktree 绝对路径 |
| `{{workspace_path}}` | string | 项目主工作区绝对路径 |
| `{{context_files}}` | string | 需要读取的 `.mac/` 文件列表，格式化为 Markdown 清单 |

### `{{context_files}}` 格式

```markdown
## 上下文文件

- STATE.json: {path}
- REQUIREMENTS.json: {path}
- SPECS.json: {path}
```

## Prompt 文件目录结构

```
prompts/
├── planner_requirements.md
├── planner_architecture.md
├── planner_specs.md
├── planner_bug_analysis.md
├── planner_doc_outline.md
├── coder.md
├── tester.md
├── reviewer.md
├── acceptance_evaluator.md
└── project_manager.md
```

每个 prompt 文件必须包含：

1. **角色定义**：你是谁、能做什么、不能做什么
2. **输入说明**：要读取哪些 `.mac/` 文件
3. **输出要求**：
   - 必须输出合法 JSON
   - 输出文件路径（写入 `TASK.json` 或 `.mac/*.json`）
   - `next_state_event` 的可选值
4. **工具限制**：允许使用哪些 Claude Code 工具
5. **沙箱规则**：只能在 worktree 内写入，沙箱外只读

## 输出 JSON Schema 传递

Orchestrator 将 `output_schema` 文件路径传给 `claude -p --json-schema`，Agent 输出必须严格符合该 schema。

## 模板渲染流程

1. Orchestrator 读取 `agent_config.json` 找到 `prompt_file` 和 `output_schema`
2. 读取基础 prompt 模板
3. 若有 skills，读取 skill 文件并追加到 system prompt
4. 替换模板变量
5. 生成临时 system prompt 文件
6. 调用 `claude -p`

## 示例

### coder.md 模板示例

```markdown
# Coder Agent

你是一个专业的 Python 开发工程师。你的任务是根据 SPECS.json 实现代码。

## 规则
- 只能在 {{worktree_path}} 内写入文件
- 不要修改 STATE.json
- 实现完成后，写入 TASK.json 的 output 字段

## 输入
{{context_files}}

## 输出要求
- 输出格式：TASK.json
- next_state_event 可选值：complete / fail / clarify / conflict / spec_question
- 必须列出所有 modified_files
```

渲染后：

```markdown
# Coder Agent

你是一个专业的 Python 开发工程师。你的任务是根据 SPECS.json 实现代码。

## 规则
- 只能在 /Users/yihanwang/multi-agent-coding-system/projects/proj_001/worktrees/coder_task_001_20260707_120500/ 内写入文件
- 不要修改 STATE.json
- 实现完成后，写入 TASK.json 的 output 字段

## 输入
## 上下文文件

- STATE.json: /Users/.../worktrees/coder_task_001_20260707_120500/.mac/STATE.json
- SPECS.json: /Users/.../worktrees/coder_task_001_20260707_120500/.mac/SPECS.json

## 输出要求
- 输出格式：TASK.json
- next_state_event 可选值：complete / fail / clarify / conflict / spec_question
- 必须列出所有 modified_files
```
## 多轮与反馈输入（v1.1）

所有 planner/coder/tester/reviewer prompt 必须包含当前阶段 `round_number`、本轮基于的产物版本、未消费用户输入和 open feedback packets。

- Planner：逐条引用用户回答的 `input_id`，在产物中区分 open_questions 与 resolved_questions。
- Coder：逐条处理 Tester/Reviewer/用户反馈，并输出 `addressed_feedback`；不得在未说明的情况下忽略 blocking feedback。
- Tester：`passed=false` 时必须给出 failure_class、evidence 和 recommended_rework_target；不得把环境故障伪装成实现缺陷。
- Reviewer：每个 issue 使用稳定 ID，标记 blocking/category；下一轮必须核验 Coder 对旧 feedback 的响应并输出 verified_feedback_ids。
- 任何 Agent 若发现反馈与已确认上游产物冲突，应请求回退到对应层，不得自行改写需求或 Spec。

## 追踪与交接要求（v1.2）

- Requirements Agent 为每条 requirement 和 acceptance criterion 生成稳定 ID，覆盖功能、安全、可靠性、性能、兼容性、可观测性、成本和数据治理。
- Architecture Agent 的每个 decision 引用受影响 REQ/AC，并输出数据模型、接口、事务/幂等、失败恢复、安全边界、迁移、回滚和可观测性。
- Specs Agent 的每项 Spec 引用 REQ/AC/DEC，dependencies 只能引用 Spec ID 且必须无环；每项包含错误场景、测试用例和 Definition of Done。
- Tester 按 AC 输出 test_evidence，明确 uncovered AC 和 unknown 环境结果；测试命令成功不等于需求覆盖完整。
- Reviewer 逐 AC 检查覆盖和违背情况，报告 stale artifacts，默认只读，不通过直接修改代码“顺手修复”。
- Acceptance Evaluator 构建逐 AC 验收矩阵；存在缺失映射、缺少证据、blocking issue、stale artifact、未消费范围变更或 unknown 测试时禁止建议 confirm。

## 所有 Agent 的 ask_user 输出（v1.3）

每个 Agent prompt 都必须声明：信息不足且选择会改变范围、契约、风险、成本或验收结果时，返回 `next_state_event=ask_user`，禁止猜测。Agent 可以在用户回答后继续 ask_user，形成多轮 grill-me。

`questions` 每项至少包含：

```json
{
  "question_id": "Q-AGENT-001",
  "question": "需要用户决定的问题",
  "reason": "为什么不能由 Agent 唯一推导",
  "blocking": true,
  "allow_free_text": true,
  "options": [
    {"key": "a", "label": "方案 A", "impact": "影响", "recommended": true},
    {"key": "b", "label": "方案 B", "impact": "影响", "recommended": false}
  ],
  "default_assumption": null,
  "affected_trace_ids": ["REQ-...", "AC-...", "DEC-...", "SPEC-..."]
}
```

Agent 专属触发条件以 `product_requirements.md` 的矩阵为准。Coder/Tester/Reviewer 的回答必须恢复给原 Agent，不得统一交给 Planner。能够从已确认产物或代码唯一推导的问题不应打断用户。

所有 Agent 输出 schema 必须复用同一 `question` 定义，而不是仅在 prompt 文本中约定。约束如下：

- 当 `next_state_event=ask_user` 时，`questions` 必填且至少一项；当事件不是 `ask_user` 时不得携带 blocking question。
- `question_id` 在同一 task/round 内唯一；重试同一语义问题时沿用幂等键，真正的追问使用新 ID 并通过 `parent_question_id` 关联。
- `blocking`、`reason`、`allow_free_text`、`options`、`default_assumption` 和 `affected_trace_ids` 由 schema 校验。
- `success` 表示本轮 Agent 正常形成了“需要提问”的结论，不等同于阶段已完成；Orchestrator 不得因为 `success=true` 跳过 input pending。

Project Manager 的路由选择问题也使用该结构，但不得伪造业务 Agent 的问题或回答。
