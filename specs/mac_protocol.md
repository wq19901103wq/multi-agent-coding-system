# `.mac/` 文件协议

> 版本：v1.0  
> 用途：定义多 Agent 系统通过项目工作区 `.mac/` 目录交换上下文和产物的标准格式。  
> 原则：所有结构化产物必须写成 JSON；所有需要人类阅读的长文本可额外写同名 `.md`。数据库中的 Project / Task / Approval / PendingUserInput 是控制层真相源；`STATE.json` 是供 Agent 读取、可由数据库重建的一致性快照。

## 目录结构

```
projects/{project_id}/
├── .mac/
│   ├── STATE.json              # 当前状态、待确认事项、运行时上下文
│   ├── REQUIREMENTS.json       # planner stage=requirements 输出
│   ├── REQUIREMENTS.md         # （可选）人类可读版本
│   ├── ARCHITECTURE.json       # planner stage=architecture 输出
│   ├── ARCHITECTURE.md         # （可选）人类可读版本
│   ├── SPECS.json              # planner stage=specs 输出
│   ├── SPECS.md                # （可选）人类可读版本
│   ├── BUG_ANALYSIS.json       # planner stage=bug_analysis 输出（bug_fix 任务）
│   ├── DOC_OUTLINE.json        # planner stage=doc_outline 输出（documentation 任务）
│   └── TASK.json               # 当前执行 Agent 的输入/输出载体
├── src/                        # 主分支代码
├── tests/                      # 主分支测试
└── worktrees/                  # Agent 独立工作区
```

## 通用规则

1. **只读规则**：Agent 只能读取 `.mac/` 中的 JSON，不能修改已有 JSON。若 Agent 需要补充上下文，只能写入 `TASK.json` 的 `output` 字段。
2. **写入规则**：Agent 对自己负责生成的产物有写权限（如 planner 写 REQUIREMENTS/ARCHITECTURE/SPECS，coder 写代码文件）。
2a. **人类可读版本规则**：
   - planner 在输出 `.json` 产物后，**可选** 生成同名 `.md` 文件（如 `REQUIREMENTS.md`）。
   - `.md` 是 `.json` 的人类可读渲染，内容必须一致；若不一致，以 `.json` 为准。
   - Orchestrator 向飞书发送卡片时，优先使用 `.md` 内容作为展示文本；无 `.md` 时回退到 `.json` 摘要。
   - 其他 Agent 不生成 `.md`。
3. **状态真相源**：`STATE.json` 由 Orchestrator 在创建 worktree 前写入，Agent 可读取但不可修改；Agent 通过 `TASK.json` 的 `next_state_event` 字段请求状态迁移。
4. **JSON Schema 校验**：所有 `.json` 文件必须能通过 `specs/schemas/` 下对应 schema 校验；校验失败时 Orchestrator 将输出视为失败并触发重试。
5. **路径约定**：所有文件路径均相对于项目根目录（`projects/{project_id}/`），使用 POSIX 路径风格。
6. **版本规则**：需求、方案、Spec 和 Agent Task 均带阶段轮次；新轮次不得覆盖旧 Task。主文件可指向最新版本，但历史版本必须可追溯。
7. **反馈规则**：跨 Agent 或用户产生的修改意见使用 feedback packet，不得只依赖易被覆盖的 `latest_rejection_feedback`。
8. **追踪规则**：REQ、AC、DEC、SPEC、TEST、REV 使用稳定 ID；详细规则见 `artifact_traceability_spec.md`。所有下游产物必须显式引用其上游 IDs 和 artifact version。
9. **失效规则**：上游 confirmed artifact 发生语义变化时，引用受影响 ID 的下游 artifact、Task 和 Approval 标记 stale/expired；旧运行结果不得 merge 或推进状态。

## Feedback Packet

```json
{
  "feedback_id": "fb_...",
  "project_id": "proj_...",
  "source_stage": "REVIEW",
  "source_task_id": "task_review_...",
  "source_round": 2,
  "kind": "quality_failure",
  "failure_class": "implementation_bug",
  "summary": "端口上界判断与 Spec 不符",
  "blocking": true,
  "recommended_rework_target": "CODING",
  "evidence": [{"file": "src/config.py", "line": 42, "description": "65536 被接受"}],
  "user_comment": "边界必须严格按 Spec",
  "status": "open",
  "resolved_by_task_id": null
}
```

1. Tester fail、Reviewer blocking issue、验收驳回、用户中途修订都生成 feedback packet。
2. 回退目标 Agent 的 TASK 输入包含所有 open feedback；Coder 必须输出 `addressed_feedback` 逐项响应。
3. Reviewer 下一轮读取原 issues 和 Coder 响应，只有核验完成才能将 feedback 标为 resolved。
4. `disputed` 不等于 resolved；若意见与已确认需求/Spec 冲突，进入对应上游 REWORK。
5. feedback 只追加状态变更，不删除历史。

## Pending User Input

Agent 运行期间的自由文本写入数据库队列，并在 `STATE.json.context.pending_user_inputs` 中提供只读摘要。每条输入具有稳定 `input_id`、发送者、观察到的状态、分类和消费状态。Orchestrator 在 Agent 返回后、merge/transition 前再次检查；范围变更或纠正类输入必须阻止该轮自动确认。

## Artifact Impact Report

需求、方案或 Spec 新版本确认后生成 impact report：列出 changed IDs、受影响产物、正在运行 Task、过期审批、建议回退层和可以安全复用的证据。影响分析完成前项目不得自动进入后续 Agent 阶段。

## `STATE.json`

由 Orchestrator 维护，描述当前状态、待人工确认事项、以及供 Agent 读取的上下文摘要。

```json
{
  "$schema": "./specs/schemas/state.json",
  "version": "1.0",
  "project_id": "proj_20260707_001",
  "current_state": "REQUIREMENT_CONFIRM_PENDING",
  "previous_state": "PLANNING_REQUIREMENTS",
  "pending_approval": {
    "approval_id": "appr_20260707_001",
    "type": "confirmation",
    "state": "REQUIREMENT_CONFIRM_PENDING",
    "question": "请确认以下需求理解",
    "options": [
      {"action": "confirm", "label": "确认", "next_state": "PLANNING_ARCHITECTURE"},
      {"action": "reject", "label": "驳回", "next_state": "PLANNING_REQUIREMENTS"}
    ],
    "payload": {}
  },
  "context": {
    "task_type": "feature",
    "description": "实现一个 config-validator CLI 工具...",
    "approval_mode": "normal",
    "retry_counts": {
      "TESTING": 0,
      "REVIEW": 0
    }
  },
  "history": [
    {"state": "INIT", "entered_at": "2026-07-07T12:00:00Z"},
    {"state": "PLANNING_REQUIREMENTS", "entered_at": "2026-07-07T12:01:00Z"}
  ]
}
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `version` | string | 是 | 协议版本，当前 `"1.0"` |
| `project_id` | string | 是 | 项目 ID |
| `current_state` | string | 是 | 当前状态机状态名 |
| `previous_state` | string | 否 | 上一个状态，用于 PAUSED 恢复 |
| `pending_approval` | object/null | 否 | 当前等待人工确认的事项 |
| `pending_approval.approval_id` | string | 是 | 审批记录 ID |
| `pending_approval.type` | string | 是 | `confirmation` 或 `input` |
| `pending_approval.state` | string | 是 | 产生该审批的状态名 |
| `pending_approval.question` | string | 是 | 展示给用户的问题 |
| `pending_approval.options` | array | 是 | 可选操作列表 |
| `pending_approval.options[].action` | string | 是 | 事件名，用于状态机 transition |
| `pending_approval.options[].label` | string | 是 | 按钮/选项显示文本 |
| `pending_approval.options[].next_state` | string | 是 | 选择后进入的下一状态 |
| `pending_approval.payload` | object | 否 | 额外展示内容 |
| `context` | object | 是 | 运行时上下文，如 task_type、description、retry_counts |
| `history` | array | 是 | 状态进入历史 |

## `REQUIREMENTS.json`

planner stage=requirements 的输出，结构化描述需求理解。

```json
{
  "$schema": "./specs/schemas/requirements.json",
  "artifact_id": "requirements",
  "version": 1,
  "round": 1,
  "status": "pending_confirmation",
  "summary": "一句话需求摘要",
  "requirements": [
    {"id": "REQ-CFG-001", "title": "配置校验", "description": "校验必填字段", "priority": "must", "kind": "functional", "source": "用户原始需求", "status": "confirmed"}
  ],
  "constraints": ["约束1"],
  "ambiguities": [
    {
      "question": "范围校验是否支持 float？",
      "impact": "影响 validator.py 中 range_check 实现",
      "default_assumption": "MVP 仅支持 integer"
    }
  ],
  "acceptance_criteria": [
    {"id": "AC-CFG-001", "requirement_ids": ["REQ-CFG-001"], "statement": "缺少必填字段时退出码为 1", "verification_method": "automated_test"}
  ],
  "assumptions": [],
  "out_of_scope": ["不做的事项"]
}
```

## `ARCHITECTURE.json`

planner stage=architecture 的输出，包含技术方案、模块划分、决策选项。

```json
{
  "$schema": "./specs/schemas/architecture.json",
  "summary": "手写 JSON 校验器，不引入 JSON Schema 库",
  "tech_stack": ["Python", "argparse", "pytest"],
  "modules": [
    {
      "name": "schema",
      "responsibility": "解析 schema.json",
      "files": ["src/config_validator/schema.py"]
    }
  ],
  "decisions": [
    {
      "id": "dec_001",
      "topic": "校验器实现方式",
      "options": [
        {"key": "handwritten", "label": "手写校验器", "description": "更轻量"},
        {"key": "json_schema", "label": "使用 JSON Schema", "description": "更标准"}
      ],
      "recommended": "handwritten",
      "reason": "MVP 展示 Spec 拆分能力"
    }
  ],
  "risks": ["风险描述"],
  "dependencies": ["Python >= 3.10"]
}
```

## `SPECS.json`

planner stage=specs 的输出，把方案拆成可执行的 Spec 列表。

```json
{
  "$schema": "./specs/schemas/specs.json",
  "artifact_id": "specs",
  "version": 1,
  "round": 1,
  "status": "pending_confirmation",
  "specs": [
    {
      "id": "spec_001",
      "title": "实现 schema 解析器",
      "description": "...",
      "requirement_ids": ["REQ-CFG-001"],
      "acceptance_criterion_ids": ["AC-CFG-001"],
      "decision_ids": ["DEC-CFG-001"],
      "acceptance_criteria": ["缺少必填字段时退出码为 1"],
      "dependencies": [],
      "affected_files": ["src/config_validator/schema.py"],
      "owners": {"implementation": "coder", "verification": "tester", "review": "reviewer"},
      "test_cases": [{"id": "TEST-CFG-001", "type": "unit", "scenario": "缺少必填字段", "expected": "失败且退出码为 1", "acceptance_criterion_ids": ["AC-CFG-001"]}],
      "definition_of_done": ["实现完成", "TEST-CFG-001 通过", "无 blocking review issue"],
      "estimated_effort": "small"
    }
  ]
}
```

## `BUG_ANALYSIS.json`

planner stage=bug_analysis 的输出（仅 bug_fix 任务）。

```json
{
  "$schema": "./specs/schemas/bug_analysis.json",
  "summary": "port 范围校验未包含 0 和 65535",
  "root_cause": "validator.py 中使用的是 1 <= port < 65535",
  "reproduction": {
    "command": "python -m config_validator config_with_port_0.json schema.json",
    "expected": "应通过",
    "actual": "报错 port 超出范围"
  },
  "fix_strategy": "将范围判断改为 0 <= port <= 65535，并增加边界测试",
  "affected_files": ["src/config_validator/validator.py", "tests/test_validator.py"],
  "risks": ["无"]
}
```

## `DOC_OUTLINE.json`

planner stage=doc_outline 的输出（仅 documentation 任务）。

```json
{
  "$schema": "./specs/schemas/doc_outline.json",
  "summary": "为项目编写 AI 助手行为守则",
  "sections": [
    {
      "id": "sec_001",
      "title": "核心纪律",
      "description": "列出严禁行为",
      "assigned_agent": "coder"
    }
  ],
  "target_audience": "AI 助手",
  "tone": "严肃、明确",
  "affected_files": ["AGENTS.md"]
}
```

## `TASK.json`

当前执行 Agent 的输入/输出载体。Orchestrator 在调用 Agent 前写入 `input`，Agent 执行后写入 `output`。

```json
{
  "$schema": "./specs/schemas/task.json",
  "version": "1.0",
  "task_id": "task_20260707_001",
  "agent_type": "coder",
  "state": "CODING",
  "input": {
    "description": "按 SPECS.json 实现 config-validator CLI",
    "spec_ids": ["spec_001", "spec_002"]
  },
  "output": {
    "success": true,
    "next_state_event": "complete",
    "modified_files": [
      {"path": "src/config_validator/validator.py", "description": "实现校验逻辑"}
    ],
    "notes": ["已实现所有 Spec"]
  }
}
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `version` | string | 是 | 协议版本 |
| `task_id` | string | 是 | 本次执行 Task ID |
| `agent_type` | string | 是 | 当前 Agent 类型 |
| `state` | string | 是 | 调用时所处的状态机状态 |
| `input` | object | 是 | Orchestrator 给 Agent 的输入 |
| `output` | object | 是 | Agent 返回的输出 |
| `output.success` | boolean | 是 | 是否成功 |
| `output.next_state_event` | string | 是 | 请求的状态机事件，如 `complete`、`fail`、`clarify` |
| `output.modified_files` | array | 否 | 变更文件列表 |
| `output.notes` | array | 否 | 实现说明 |

## Orchestrator 与 Agent 的读写职责

### 主动提问协议

任一 Agent 可返回 `output.next_state_event = "ask_user"` 和 `output.question_batch.questions`。Orchestrator 为每个问题分配/校验稳定 question_id，持久化 `resume_state`、agent_type、stage、task_id、round、artifact versions 和 trace IDs，并创建 input approval。

回答记录只追加，至少包含 `question_id`、answer kind（option/free_text/accept_default）、selected keys、text、answered_by、source_chat_id 和 answered_at。新一轮 Agent 读取全部历史问答及仍未解决问题；blocking question 未解决时协议层拒绝 `complete`。

| 文件 | Orchestrator 写 | Orchestrator 读 | Agent 写 | Agent 读 |
|------|----------------|----------------|---------|---------|
| `STATE.json` | ✅ 创建/更新 | ✅ | ❌ | ✅ |
| `REQUIREMENTS.json` | ❌ | ✅ | ✅ planner stage=requirements | ✅ 非 planner |
| `ARCHITECTURE.json` | ❌ | ✅ | ✅ planner stage=architecture | ✅ 非 planner |
| `SPECS.json` | ❌ | ✅ | ✅ planner stage=specs | ✅ coder/tester/reviewer |
| `BUG_ANALYSIS.json` | ❌ | ✅ | ✅ planner stage=bug_analysis | ✅ coder/tester/reviewer |
| `DOC_OUTLINE.json` | ❌ | ✅ | ✅ planner stage=doc_outline | ✅ coder/reviewer |
| `TASK.json` | ✅ input | ✅ output | ✅ output | ✅ input |

## `.mac/` 同步时序

```
Orchestrator                    worktree                    Agent
   |                               |                         |
   |-- 1. 写 STATE.json/TASK.json -->|                       |
   |-- 2. 复制 .mac/ 到 worktree -->|                       |
   |                               |-- 3. Agent 读取并执行 -->|
   |                               |<-- 4. Agent 写 TASK.json -|
   |<-- 5. 读取 TASK.json output --|                       |
   |-- 6. 更新主工作区 .mac/ ----->|                       |
```
