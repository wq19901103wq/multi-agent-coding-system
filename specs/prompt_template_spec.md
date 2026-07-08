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
