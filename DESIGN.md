# Multi-Agent Coding System 架构设计与 MVP 实现计划

> **现行规范说明（v3.2）**：涉及状态转换、多轮确认、自动审批、用户中途输入和跨 Agent 反馈时，以 `specs/state_machine_semantics.md`、`specs/orchestrator_spec.md`、`specs/mac_protocol.md` 和 `specs/approval_modes.json` 为准。本文后续保留的早期流程图和阶段计划仅作为背景，不覆盖上述规范。

> 需求、架构、Spec、测试和验收之间的稳定 ID、版本、失效传播及验收矩阵以 `specs/artifact_traceability_spec.md` 为准。

> Claude Code Runtime 的工具执行采用单一所有者模型：Read/Write/Edit/Bash 等内置工具由 Claude Code CLI 执行；MAC 只负责进程生命周期、权限策略、事件观察、进度转发和最终结果解析。MAC 不得根据 `tool_use` 再执行一次工具或伪造 `tool_result`。详细约束以 `specs/runtime_spec.md` 为准。

## 1. 设计目标

构建一个**条件人类闭环（Conditional Human-in-the-Loop）**的多 Agent 编码系统：

- 覆盖完整软件交付流程：需求分析 → 技术规划 → Spec 拆分 → 代码实现 → 测试 → Review → 验收 → 自动合并提交 Git
- 4 个关键节点必须人工确认：需求确认、技术方案确认、Agent 澄清/冲突、验收确认
- 移动端通过飞书机器人完成确认、追问、状态查看
- 验收失败时能根因定位：是需求、Spec、实现还是测试的问题
- MVP 任务：3 个真实任务
  1. **Feature**：实现 `config-validator` CLI
  2. **Bug Fix**：修复 `config-validator` 的 port 范围校验 bug
  3. **Documentation**：为项目写一份 `AGENTS.md`
- 运行模式：真实 Claude Code + 真实飞书机器人 + 真实 Git（不用 Mock）

---

## 2. 系统架构（综合 Overstory + Agent Vault + Claude MPM 等最佳实践）

### 2.1 高层组件

```
┌─────────────────────────────────────────────────────────────────┐
│                         飞书 App (移动端)                        │
│              确认卡片 / 冲突卡片 / 状态仪表盘 / 审批链接            │
└─────────────────────────────┬───────────────────────────────────┘
                              │ 消息 / 卡片 / 事件回调
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Feishu Bridge (Python)                       │
│  - 接收飞书事件 (lark-oapi)                                     │
│  - 发送/更新卡片消息                                             │
│  - 用户身份映射                                                  │
└─────────────────────────────┬───────────────────────────────────┘
                              │ HTTP API
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Control API (FastAPI)                         │
│  - 启动新任务 / 查询状态 / 提交人工决策                             │
│  - 状态机驱动                                                    │
└─────────────────────────────┬───────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│   Orchestrator  │ │   SQLite DB     │ │   Git Operator  │
│(Python Orchestrator)│ │ 状态 + 产物 +  │ │   (GitPython)   │
│                 │ │   审计日志      │ │                 │
└────────┬────────┘ └─────────────────┘ └─────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Agent Runtime Layer                          │
│  ┌────────────────┐ ┌────────────────┐ ┌────────────────┐       │
│  │ ClaudeCode     │ │ ZCode          │ │ KimiCode       │       │
│  │ Runtime        │ │ Runtime        │ │ Runtime        │       │
│  │ (claude -p)    │ │ (placeholder)  │ │ (placeholder)  │       │
│  └────────────────┘ └────────────────┘ └────────────────┘       │
│  ┌────────────────┐ ┌────────────────┐                           │
│  │ Codex Runtime  │ │ (reserved)     │                           │
│  │ (placeholder)  │ │ (for testing)  │                           │
│  └────────────────┘ └────────────────┘                           │
└─────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                 Specialist Agent Instances                       │
│  每个 Agent 在独立 git worktree 中执行，结果合并回主工作区           │
└─────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Project Workspace                            │
│  projects/{project_id}/                                          │
│  ├── .mac/          # 上下文文件（File-Based Handoff）             │
│  ├── src/           # 主分支代码                                  │
│  ├── tests/         # 主分支测试                                  │
│  └── worktrees/     # 各 Agent 的独立工作区                        │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 借鉴的设计模式

| 来源项目 | 借鉴的模式 | 我们的实现 |
|---------|-----------|-----------|
| **Overstory** | git worktree 隔离 + SQLite 协调 | 每个 Agent 在独立 worktree 执行，结果合并 |
| **Agent Vault** | 手机审批链接 + 审计日志 | 飞书卡片/链接审批，SQLite 记录所有决策 |
| **Claude MPM** | 围绕 Claude Code 的项目管理 | Orchestrator 管理项目生命周期 |
| **oh-my-claudecode** | Review Gate | Review 不通过阻塞流程 |
| **Rune Orchestration** | File-Based Handoff | `.mac/` 目录作为标准上下文传递协议 |
| **lark-channel-bridge** | 飞书卡片流式更新 | 状态卡片持续更新，避免刷屏 |

### 2.3 关键设计原则

1. **状态机是核心**：Agent Runtime 负责执行，人工确认/异常/冲突由外部状态机控制
2. **Agent Runtime 可插拔**：Claude Code / ZCode / KimiCode / Codex 统一接口（MVP 仅实现 Claude Code）
3. **Git Worktree 隔离**：每个 Agent 在独立 worktree 执行，避免文件冲突
4. **File-Based Handoff**：Agent 通过 `.mac/` 上下文文件传递状态
5. **产物血缘 + 审计日志**：所有输入、输出、决策写入 SQLite
6. **飞书只负责交互**：不承载业务逻辑，所有状态变更通过 Control API
7. **Git 操作自动**：验收通过后自动 commit + push 到 feature 分支，不自动 merge 到 main

### 2.4 AgentRuntime 接口（可插拔执行层）

```python
class AgentRuntime(ABC):
    @abstractmethod
    async def execute(
        self,
        agent_type: str,              # e.g. "coder"
        task_prompt: str,             # 任务描述
        system_prompt: str,           # 角色定义
        workspace: Path,              # 主工作区路径
        worktree: Path,               # 本次执行的 worktree 路径
        context_files: list[Path],    # 需要读取的 .mac/ 上下文文件
        output_schema: dict,          # 期望输出的 JSON schema
        allowed_tools: list[str],     # 允许使用的工具
        readonly_dirs: list[Path],    # 沙箱外允许只读访问的目录
    ) -> AgentRuntimeResult:
        ...

@dataclass
class AgentRuntimeResult:
    success: bool
    output_json: dict | None        # 结构化输出
    output_files: list[Path]        # Agent 写入的文件
    logs: str                       # 执行日志
    cost_info: dict | None          # token 消耗等信息
```

**当前实现**：
- `ClaudeCodeRuntime`：调用 `claude -p --output-format json`，通过 `--add-dir` 传入 `readonly_dirs`

**预留实现**：
- `ZCodeRuntime`、`KimiCodeRuntime`、`CodexRuntime`

### 2.5 Git Worktree 隔离与合并策略

每个 Agent 执行时：

1. Orchestrator 从主工作区创建临时 git worktree
   ```bash
   git worktree add projects/{project_id}/worktrees/coder_spec_001_20260707_001 -b feat/coder_spec_001_20260707_001
   ```
2. Agent Runtime 在 worktree 中执行
3. Agent 修改 worktree 中的文件
4. Tester 在 worktree 中运行测试
5. **Reviewer Agent 按顺序合并 worktree**：
   - Reviewer  review 当前 worktree 的变更
   - 如果通过：Reviewer 决定合并，Git Operator 执行 `git merge` 到主工作区
   - 如果不通过：worktree 保留，状态回退到 CODING
6. 清理已合并或失败的 worktree

**合并策略**：
- **顺序合并**：一个 worktree  review 通过后再处理下一个
- **不检查 affected_files 重叠**：不同 Spec 改同一个文件是正常的，冲突由 git merge 处理
- **Reviewer Agent 负责合并决策**：Reviewer 既 review 代码，也决定是否合并回主工作区
- **冲突处理**：如果 git merge 产生冲突，Reviewer Agent 尝试解决；解决不了则升级到人工冲突裁决

### 2.6 Quality Gates（质量门）

借鉴 oh-my-claudecode 的 review gate：

| Gate | 触发时机 | 失败处理 |
|------|---------|---------|
| **需求理解门** | planner stage=requirements 输出后 | 人类确认/澄清 |
| **方案设计门** | planner stage=architecture 输出后 | 人类确认/修改 |
| **Spec 合理性门** | planner stage=specs 输出后 | 人类确认/调整 |
| **代码实现门** | Coder 完成后，Tester 测试前 | 用户修订进入 REWORK |
| **测试门** | Tester 完成后 | quality fail 进入 REWORK 并按根因路由 |
| **Review 门** | Reviewer 完成后 | blocking issue 进入 REWORK 并注入下一轮 Agent |
| **验收门** | Reviewer 完成后 | 人类最终确认 |

### 2.7 安全模型

**沙箱边界**：
- **沙箱目录** = `projects/{project_id}/` 及其 worktree
- 沙箱内 Agent 拥有完整读写执行权限
- 沙箱外 Agent 只有读权限

**MVP 实现**：
- Agent Runtime 以沙箱目录为工作目录运行
- 通过 `--add-dir` 允许读取其他只读目录（如参考代码库）
- 所有写操作必须在沙箱内
- 所有文件操作写入审计日志

**后续增强**：
- 容器化沙箱（Docker）
- 文件系统级权限控制
- 网络访问限制

### 2.8 Audit Log（审计日志）

借鉴 Agent Vault：

- 所有 Agent 执行记录：`executions` 表
- 所有人工决策记录：`decisions` 表
- 所有飞书消息记录：`messages` 表
- 所有 git 操作记录：`git_operations` 表
- 所有权限/工具调用记录：`agent_tool_calls` 表

审计日志不可被 Agent 修改（只追加），支持事后复盘。

---

## 3. Agent 角色设计（多 Runtime 实例）

采用 **多 Runtime 实例** 模式：
- 每个 Specialist Agent 都是一次 AgentRuntime 调用（默认 Claude Code）
- Project Manager 也是 AgentRuntime 调用，但职责是生成摘要和回答追问
- Orchestrator（Python 控制层）负责任务分派、worktree 管理和状态推进
- Agent 之间通过 `.mac/` 上下文文件和 git worktree 协作

### 3.1 Manager Agent：Project Manager

- **目标**：理解上下文、推荐下一步动作、生成供人类确认的内容摘要
- **职责**（只生成内容，不控制系统状态）：
  - 解析原始需求
  - 汇总 planner 各阶段输出，生成「需求确认」「方案确认」「Spec 确认」摘要
  - 协调 coder、tester、reviewer 之间的任务依赖
  - 识别是否需要人类澄清或冲突裁决，并生成问题描述
  - 汇总最终产物，生成「验收确认」摘要
  - 回答用户追问

- **不职责**：
  - 不直接发送飞书卡片（由 `bridge/feishu.py` 负责）
  - 不推进状态机（由 `core/state_machine.py` 负责）
  - 不写数据库（由 `core/project_service.py` 负责）

### 3.2 Worker Agents

| Agent | 职责 | 输出产物 |
|-------|------|---------|
| **planner** | 分三阶段完成需求分析、技术方案设计、Spec 拆分 | `.mac/REQUIREMENTS.json` / `ARCHITECTURE.json` / `SPECS.json` |
| **coder** | 按 Spec 实现代码/文档 | 代码或文档文件 |
| **tester** | 写测试用例并执行 | `tests/` + 测试报告 |
| **reviewer** | Review 代码和测试 | Review 报告 |
| **acceptance_evaluator** | 汇总所有产物，判断是否符合需求 | 验收报告 |

> 说明：原设计中的 Requirements Analyst、Architect、Spec Writer 合并为 **planner** 的三个 stage（requirements / architecture / specs），由同一个 `planner` Agent 顺序执行，以共享 prompt cache 并简化角色管理。

### 3.3 Claude Code 执行层

每个 Agent 的调用方式：

```bash
claude -p "<role-specific prompt>" \
  --output-format json \
  --json-schema /path/to/output_schema.json \
  --dangerously-skip-permissions \
  --allowedTools "Read,Edit,Write,Bash" \
  --add-dir /path/to/readonly_reference_dir \
  --system-prompt-file /path/to/agent_system_prompt.md
```

**调用参数说明**：
- `-p`：非交互模式，执行完即退出
- `--output-format json`：返回结构化 JSON，便于 Orchestrator 解析
- `--json-schema`：强制输出格式
- `--dangerously-skip-permissions`：自动化必需，MVP 在本地 Mac 使用
- `--allowedTools`：限制工具范围，降低风险
- `--system-prompt-file`：加载 Agent 角色定义

### 3.4 Agent 上下文文件协议 + Worktree 模型

每个项目工作区 `projects/{project_id}/` 包含：

```
workspace/
├── .mac/                       # 共享上下文（File-Based Handoff）
│   ├── TASK.md
│   ├── REQUIREMENTS.md
│   ├── REQUIREMENTS.json
│   ├── ARCHITECTURE.md
│   ├── ARCHITECTURE.json
│   ├── SPECS.md
│   ├── SPECS.json
│   ├── STATE.json              # 当前状态和待确认事项
│   ├── DECISION_LOG.md         # 人工决策记录
│   └── AUDIT_LOG.md            # 执行审计摘要
├── src/                        # 主分支代码
├── tests/                      # 主分支测试
├── README.md
├── pyproject.toml
└── worktrees/                  # 各 Agent 的独立工作区
    ├── planner_requirements_20260707_001/
    ├── planner_architecture_20260707_001/
    ├── planner_specs_20260707_001/
    ├── coder_spec_001_20260707_001/
    ├── tester_spec_001_20260707_001/
    └── reviewer_spec_001_20260707_001/
```

**Agent 调用流程**：

1. Orchestrator 根据当前状态确定要调用哪个 Agent（如 `planner`、`coder`）
2. 从主工作区创建临时 git worktree
3. 准备 `STATE.json` 和必要的 `.mac/` 上下文文件（同步到 worktree）
4. 生成该 Agent 的 prompt 和 JSON schema
5. 调用 AgentRuntime 在 worktree 中执行；`readonly_dirs` 仅包含沙箱外必须参考的目录
6. 读取 Agent 返回的 JSON 和写入 worktree 的文件
7. 评估结果：
   - 成功：将变更合并回主工作区
   - 失败：保留 worktree 用于调试，状态回退
8. 清理或保留 worktree
9. 更新数据库和状态机



## 4. 状态机

系统采用“一个通用状态机 + 多个 Workflow Profile”。`state_machines/delivery.json` 是唯一生命周期真相源，定义各类阶段共享的执行、澄清、确认、失败、REWORK、验收和终态语义；`workflow_profiles/*.json` 只声明不同 task type 的阶段组成和允许回退目标。运行时将两者编译成具体、可读的业务状态名，现有 API 和飞书卡片无需迁移为抽象状态名。

```text
delivery.json                       workflow_profiles/feature.json
  stage policies        +             specify(requirements + behavior)
  REWORK / acceptance                 → architecture → specs
  merge / terminal                    → coding → testing → review
                       ↓ compile
  PLANNING_REQUIREMENTS → ... → REVIEW_CONFIRM_PENDING
```

禁止在 Profile 中复制完整 `states`，也禁止在 Orchestrator 中按 task type 重新硬编码 REWORK/验收路由。新增任务类型优先新增 Profile；只有所有任务的生命周期都要改变时才修改 `delivery.json`。

### 4.1 状态定义

```
INIT                           # 任务创建
PLANNING_REQUIREMENTS          # Specify：需求、产品行为和验收标准
REQUIREMENT_CONFIRM_PENDING    # 等待用户确认统一需求规格
PLANNING_ARCHITECTURE          # 技术方案设计中（planner stage=architecture）
ARCHITECTURE_CONFIRM_PENDING   # 等待用户确认方案
PLANNING_IMPLEMENTATION_SPECS                 # Spec 拆分中（planner stage=specs）
IMPLEMENTATION_SPEC_CONFIRM_PENDING           # 等待用户确认 Spec
CLARIFICATION_PENDING          # Agent 请求澄清，等待用户
CONFLICT_PENDING               # 设计冲突，等待用户裁决
CODING                         # 编码中
TESTING                        # 测试中
REVIEW                         # Review 中
ACCEPTANCE_PENDING             # 等待用户验收
MERGING                        # 自动合并中
COMPLETED                      # 完成
FAILED                         # 失败终止
CANCELLED                      # 已取消
PAUSED                         # 已暂停
```

### 4.2 关键状态转换

```
INIT → PLANNING_REQUIREMENTS
PLANNING_REQUIREMENTS → REQUIREMENT_CONFIRM_PENDING
REQUIREMENT_CONFIRM_PENDING → PLANNING_ARCHITECTURE (用户确认)
REQUIREMENT_CONFIRM_PENDING → PLANNING_REQUIREMENTS (用户驳回)
PLANNING_ARCHITECTURE → ARCHITECTURE_CONFIRM_PENDING
ARCHITECTURE_CONFIRM_PENDING → PLANNING_IMPLEMENTATION_SPECS (用户确认)
ARCHITECTURE_CONFIRM_PENDING → PLANNING_ARCHITECTURE (用户驳回)
PLANNING_IMPLEMENTATION_SPECS → IMPLEMENTATION_SPEC_CONFIRM_PENDING
IMPLEMENTATION_SPEC_CONFIRM_PENDING → CODING (用户确认)
IMPLEMENTATION_SPEC_CONFIRM_PENDING → PLANNING_IMPLEMENTATION_SPECS (用户驳回)
CODING → CLARIFICATION_PENDING (Agent 请求澄清)
CLARIFICATION_PENDING → CODING (用户回答后继续)
CLARIFICATION_PENDING → PLANNING_IMPLEMENTATION_SPECS (需要改 Spec)
CLARIFICATION_PENDING → PLANNING_ARCHITECTURE (需要改方案)
CODING → CONFLICT_PENDING (设计冲突)
CONFLICT_PENDING → CODING (用户裁决后继续)
CONFLICT_PENDING → PLANNING_ARCHITECTURE (回退到方案层)
CODING → CODING_CONFIRM_PENDING → TESTING
TESTING → TESTING_CONFIRM_PENDING → REVIEW
TESTING → REWORK (测试失败)
REVIEW → REVIEW_CONFIRM_PENDING → ACCEPTANCE_PENDING
REVIEW → REWORK (blocking issue)
ACCEPTANCE_PENDING → MERGING (用户验收通过)
ACCEPTANCE_PENDING → PLANNING_REQUIREMENTS / PLANNING_ARCHITECTURE / PLANNING_IMPLEMENTATION_SPECS / CODING (用户驳回，根据根因选择回退阶段)
MERGING → COMPLETED
MERGING → FAILED
```

### 4.3 Pending 状态恢复机制

当流程进入 `*_PENDING` 状态时：

1. **系统记录待确认事项**：将摘要、选项、相关 artifact_id 写入 `decisions` 表
2. **飞书发送确认卡片**：用户在手机上决策或追问
3. **用户决策后**：Control API 接收决策，调用 `StateMachine.transition()`
4. **Project Manager Agent 不负责状态推进**：它只根据用户决策生成新的上下文或推荐，状态变更由 Python 控制层执行
5. **恢复执行**：状态回到执行态后，Agent Orchestrator 读取最新上下文继续

**追问的特殊处理**：
- 用户在卡片下回复视为「追问」
- 追问不直接改变状态
- Project Manager Agent 生成回答后，更新原卡片内容
- 用户最终选择确认/驳回/选择方案后才触发状态转换

---

## 5. Control API 接口定义

### 5.1 核心端点

| 方法 | 路径 | 用途 | 关键字段 |
|------|------|------|---------|
| `POST` | `/projects` | 创建新项目 | `name?`, `task_type`, `description`, `approval_mode?`, `bound_directory?` |
| `GET` | `/projects/{project_id}` | 查询项目状态和产物 | - |
| `GET` | `/projects/{project_id}/status` | 查询当前状态 | - |
| `POST` | `/projects/{project_id}/decisions` | 提交人工决策 | `state`, `decision`, `comment?`, `selected_option?` |
| `POST` | `/projects/{project_id}/messages` | 用户追问 | `content` |
| `POST` | `/webhook/feishu` | 接收飞书事件回调 | 飞书原始 payload |
| `POST` | `/admin/trigger/{project_id}` | 手动触发下一步（调试用） | `action` |

### 5.2 请求/响应示例

#### 创建项目

**Request：**
```json
POST /projects
{
  "name": "config-validator-cli",
  "task_type": "feature",
  "description": "实现一个 config-validator CLI 工具，读取 config.json 和 schema.json，校验必填字段、类型、数值范围，输出 text 或 json 格式。",
  "approval_mode": "normal",
  "bound_directory": "/absolute/path/to/existing-repository"
}
```

**Response：**
```json
{
  "project_id": "proj_20260707_001",
  "status": "INIT",
  "workspace_path": "/absolute/path/to/existing-repository",
  "bound_directory": "/absolute/path/to/existing-repository",
  "created_at": "2026-07-07T12:00:00Z"
}
```

#### 提交人工决策

**Request：**
```json
POST /projects/proj_20260707_001/decisions
{
  "state": "REQUIREMENT_CONFIRM_PENDING",
  "decision": "confirm",
  "comment": "确认，范围校验也做"
}
```

**Response：**
```json
{
  "project_id": "proj_20260707_001",
  "previous_state": "REQUIREMENT_CONFIRM_PENDING",
  "current_state": "PLANNING_ARCHITECTURE",
  "updated_at": "2026-07-07T12:05:00Z"
}
```

#### 用户追问

**Request：**
```json
POST /projects/proj_20260707_001/messages
{
  "content": "数据库为什么选 SQLite？"
}
```

**Response：**
```json
{
  "project_id": "proj_20260707_001",
  "reply": "SQLite 零配置，适合 MVP 阶段验证。后续可迁移到 PostgreSQL。",
  "state": "ARCHITECTURE_CONFIRM_PENDING"
}
```

### 5.3 错误响应

统一错误格式：
```json
{
  "error_code": "INVALID_STATE_TRANSITION",
  "message": "Cannot confirm requirement from state PLANNING_ARCHITECTURE",
  "current_state": "PLANNING_ARCHITECTURE"
}
```

---

## 6. 数据模型（SQLite）

### 6.1 表结构

MVP 阶段采用精简模型，产物内容通过 `context` / `input_data` / `output_data` JSON 字段灵活存储，避免过度拆分。

```sql
-- 项目
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT,
    task_type TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL,
    current_state TEXT NOT NULL,
    previous_state TEXT,
    workspace_path TEXT NOT NULL,
    bound_directory TEXT,       -- 创建时继承的已有仓库快照
    context TEXT,              -- JSON：需求、方案、Spec 等产物
    approval_config TEXT,      -- JSON：审批模式配置
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Agent 执行记录（一次状态机推进对应一条 Task）
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id),
    agent_type TEXT NOT NULL,
    state TEXT NOT NULL,
    status TEXT NOT NULL,      -- pending / running / completed / failed
    worktree_path TEXT,
    input_data TEXT,           -- JSON
    output_data TEXT,          -- JSON
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    cost_usd REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 人工确认/输入记录
CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id),
    task_id TEXT REFERENCES tasks(id),
    state TEXT NOT NULL,
    approval_type TEXT NOT NULL,  -- confirmation / input
    question TEXT,
    payload TEXT,              -- JSON：卡片展示内容
    response TEXT,
    responded_by TEXT,
    status TEXT NOT NULL,      -- pending / responded / expired
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    responded_at TIMESTAMP
);

-- 审计日志
CREATE TABLE event_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT REFERENCES projects(id),
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    meta TEXT,                 -- JSON
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 飞书用户-项目绑定（MVP 阶段一个项目通常绑定创建者）
CREATE TABLE project_users (
    project_id TEXT REFERENCES projects(id),
    open_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project_id, open_id)
);

-- 聊天默认代码仓库；与每个用户的 active project 分离
CREATE TABLE chat_directory_bindings (
    chat_id TEXT PRIMARY KEY,
    directory TEXT NOT NULL,
    bound_by TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 一轮 Agent 主动提问；具体 Question/QuestionAnswer 另表持久化
CREATE TABLE question_interactions (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id),
    origin_task_id TEXT REFERENCES tasks(id),
    origin_round INTEGER NOT NULL,
    resume_state TEXT NOT NULL,
    agent TEXT NOT NULL,
    stage TEXT NOT NULL,
    batch_key TEXT NOT NULL,
    status TEXT NOT NULL,
    approval_id TEXT REFERENCES approvals(id)
);
```

### 6.2 产物血缘

通过 `executions.input_artifact_ids` 和 `executions.output_artifact_ids` 建立 DAG，支持从最终代码回溯到需求和方案。

### 6.3 LLM 输出契约

为保证 Claude Code Agent 输出可被 Python 控制层解析，每个阶段必须输出固定格式。

> 本节保留的 JSON 仅用于说明字段意图，不再是可执行完整样例。v3.2 的唯一结构契约是 `specs/schemas/*.json`；REQ/AC/DEC/SPEC/TEST 追踪和验收矩阵见 `specs/artifact_traceability_spec.md`。实现和 Prompt 不得复制本节旧的字符串 AC、单一 assigned_agent 或总体 meets_requirements 格式。

#### planner stage=requirements 输出

```json
{
  "summary": "一句话需求摘要",
  "goals": ["目标1", "目标2"],
  "constraints": ["约束1", "约束2"],
  "ambiguities": [
    {
      "question": "范围校验是否支持 float？",
      "impact": "影响 validator.py 中 range_check 实现"
    }
  ],
  "acceptance_criteria": ["标准1", "标准2"],
  "assumptions": ["假设1"]
}
```

#### planner stage=architecture 输出

```json
{
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
      "topic": "校验器实现方式",
      "option_a": "手写",
      "option_b": "JSON Schema",
      "chosen": "option_a",
      "reason": "MVP 展示 Spec 拆分能力"
    }
  ],
  "risks": ["风险描述"]
}
```

> 说明：`decisions` 列表中的选项会在 `ARCHITECTURE_CONFIRM_PENDING` 阶段渲染为飞书卡片按钮，用户可直接选择预定义修改项。

#### planner stage=specs 输出

```json
{
  "specs": [
    {
      "id": "spec_001",
      "title": "实现 schema 解析器",
      "description": "...",
      "acceptance_criteria": ["..."],
      "dependencies": [],
      "affected_files": ["src/config_validator/schema.py"],
      "assigned_agent": "coder"
    }
  ]
}
```

#### Coder 输出

```json
{
  "modified_files": [
    {
      "path": "src/config_validator/validator.py",
      "description": "实现校验逻辑"
    }
  ],
  "notes": ["实现说明"]
}
```

#### Tester 输出

```json
{
  "test_files": ["tests/test_validator.py"],
  "coverage": 0.92,
  "passed": true,
  "failures": [],
  "report": "pytest 输出摘要"
}
```

#### Reviewer 输出

```json
{
  "result": "pass",
  "issues": [
    {
      "severity": "minor",
      "file": "src/config_validator/validator.py",
      "line": 42,
      "description": "建议增加边界条件处理"
    }
  ],
  "summary": "代码符合要求， minor issue 不影响功能"
}
```

#### Acceptance Evaluator 输出

```json
{
  "result": "pending_human_review",
  "summary": "功能已实现，测试通过，等待用户验收",
  "evidence": {
    "tests_passed": true,
    "coverage": 0.92,
    "review_result": "pass",
    "meets_requirements": true
  }
}
```

---

## 7. 飞书交互设计

### 7.1 消息类型

| 类型 | 用途 | 交互元素 |
|------|------|---------|
| **确认卡片** | 需求确认、方案确认、验收确认 | 确认 / 驳回 / 评论按钮 |
| **冲突卡片** | 展示设计冲突双方观点 | 选择 A / 选择 B / 追问 |
| **状态卡片** | 展示任务整体进展 | 刷新 / 查看详情 |
| **报告卡片** | 展示验收报告 / 测试报告 | 展开详情 / 下载 |
| **通知消息** | 阶段开始/结束、异常提醒 | 纯文本 |

### 7.2 追问机制

用户在卡片下回复消息，系统识别为对当前待确认事项的追问。

```
用户：数据库为什么选 SQLite 而不是 PostgreSQL？
系统：Project Manager 收到追问，重新分析后回复...
系统：回复消息（解释原因）
用户：同意
系统：状态推进到 PLANNING_IMPLEMENTATION_SPECS
```

### 7.3 卡片更新策略

- 每个待确认事项发送一条独立卡片
- 用户决策后，卡片状态更新为「已确认 / 已驳回 / 已评论」
- 状态卡片使用同一条消息持续更新，避免刷屏

### 7.4 飞书卡片 JSON 结构示例

#### 需求确认卡片

```json
{
  "config": {"wide_screen_mode": true},
  "header": {
    "title": {"tag": "plain_text", "content": "需求确认：config-validator CLI"},
    "template": "blue"
  },
  "elements": [
    {"tag": "div", "text": {"tag": "lark_md", "content": "**摘要**：实现一个 JSON 配置校验器 CLI"}},
    {"tag": "div", "text": {"tag": "lark_md", "content": "**目标**：1. 读取 config.json 2. 校验必填/类型/范围 3. 输出 text/json"}},
    {"tag": "div", "text": {"tag": "lark_md", "content": "**模糊点**：范围校验是否支持 float？"}},
    {"tag": "action", "actions": [
      {"tag": "button", "text": {"tag": "plain_text", "content": "确认"}, "type": "primary", "value": {"action": "confirm", "state": "REQUIREMENT_CONFIRM_PENDING"}},
      {"tag": "button", "text": {"tag": "plain_text", "content": "驳回"}, "type": "danger", "value": {"action": "reject", "state": "REQUIREMENT_CONFIRM_PENDING"}}
    ]}
  ]
}
```

#### 冲突裁决卡片

```json
{
  "config": {"wide_screen_mode": true},
  "header": {
    "title": {"tag": "plain_text", "content": "设计冲突：校验器实现方式"},
    "template": "red"
  },
  "elements": [
    {"tag": "div", "text": {"tag": "lark_md", "content": "**Coder 观点**：手写校验器，更轻量"}},
    {"tag": "div", "text": {"tag": "lark_md", "content": "**Reviewer 观点**：用 JSON Schema，更标准"}},
    {"tag": "action", "actions": [
      {"tag": "button", "text": {"tag": "plain_text", "content": "选手写"}, "type": "primary", "value": {"action": "resolve", "state": "CONFLICT_PENDING", "option": "handwritten"}},
      {"tag": "button", "text": {"tag": "plain_text", "content": "选 JSON Schema"}, "type": "primary", "value": {"action": "resolve", "state": "CONFLICT_PENDING", "option": "json_schema"}}
    ]}
  ]
}
```

#### 事件回调解析

飞书回调 payload 关键字段：
```json
{
  "header": {
    "event_id": "...",
    "token": "...",
    "create_time": "..."
  },
  "event": {
    "message": {
      "message_id": "...",
      "chat_id": "...",
      "sender": {"sender_id": {"open_id": "..."}},
      "content": "{\"text\":\"确认\"}"
    }
  }
}
```

系统先根据卡片/命令中的显式 ID 或 `(chat_id, open_id)` 的 ChatUserContext 确定 project_id，再解析用户动作。禁止通过 open_id 选择“最近更新项目”。

**用户-项目绑定机制**：
- 创建项目时，系统记录发起人的 `open_id`（若从飞书创建）或允许用户在创建后通过飞书发送绑定指令（如 `/bind proj_xxx`）完成绑定。
- `project_users` 维护权限及角色；`project_chats` 维护通知/可见关系；`chat_user_contexts` 维护每个用户在每个聊天中的 active project。
- 一个聊天可关联多个项目，一个项目也可订阅多个聊天；项目状态不属于聊天。
- 卡片 approval_id、显式 project_id 的优先级高于 active context；无唯一上下文时要求用户选择。
- 完整规则见 `specs/conversation_routing_spec.md`。

---

## 8. 人工确认流程

### 8.0 审批模式配置

系统支持多种审批模式，创建项目时通过 `approval_mode` 字段选择预设模式名。具体规则由系统配置（环境变量或全局配置）决定，不在每个项目中重复存储完整配置。

**预设模式**：

| 模式 | 需求确认 | 方案确认 | Spec 确认 | 澄清/冲突 | 验收确认 | 适用场景 |
|------|---------|---------|----------|----------|---------|---------|
| **strict** | ✅ | ✅ | ✅ | ✅ | ✅ | 关键项目、不熟悉的需求 |
| **normal** | ✅ | ✅ | ✅ | ⚠️ 仅异常 | ✅ | 默认模式 |
| **night** | ✅ | ❌ | ❌ | ⚠️ 仅异常 | ✅ | 夜间自动放宽 |
| **auto** | ❌ | ❌ | ❌ | ⚠️ 仅异常 | ✅ | 高信任度任务 |

**规则**：
- 人工确认长期未响应时保持 pending，不占用 Agent 执行槽位；只有显式 `/pause` 才进入暂停控制状态
- 夜间模式可自动切换，减少打扰
- 澄清/冲突默认需要人工，除非配置为自动

### 8.1 需求确认

**呈现内容**：
- 原始需求摘要
- 结构化理解（目标、用户、场景、约束）
- 识别出的模糊点
- 初步验收标准

**用户选项**：
- 确认
- 驳回 + 评论
- 追问

### 8.2 技术方案确认

**呈现内容**：
- 一句话方案摘要
- 技术选型及理由
- 模块划分
- 影响范围
- 风险点

**用户选项**：
- 确认
- 选择预定义修改项（系统根据方案中的 `decisions` 列表生成按钮，如「换用 JSON Schema」「去掉范围校验」）
- 驳回重写
- 追问

**关于自然语言局部修改指令**：
- MVP 阶段不支持任意自然语言修改指令（如「范围校验先不做」）
- 原因：解析不准确会导致方案跑偏
- 如果需要支持，需要单独设计「指令解析 Agent」，作为后续增强

### 8.3 澄清 / 冲突裁决

**呈现内容**：
- 问题背景
- 双方观点或可选方案
- 各自理由

**用户选项**：
- 选择 A
- 选择 B
- 给出新指令
- 追问

### 8.3a Worktree 合并冲突裁决

当 Reviewer Agent 合并 worktree 遇到 git 冲突且无法自动解决时：

**呈现内容**：
- 冲突文件列表
- 冲突双方的变更摘要
- Reviewer Agent 的合并建议

**用户选项**：
- 接受 Reviewer 建议的合并方案
- 给出新指令
- 追问
- 驳回，让 Coder 重新实现

### 8.4 验收确认

**呈现内容**：
- 测试是否通过
- 覆盖率
- Review 结果
- 关键变更文件列表
- 是否符合原始需求

**用户选项**：
- 验收通过 → 自动合并
- 验收失败 → 选择回退阶段（需求/方案/实现/测试）
- 追问

---

## 9. 根因定位设计

验收失败时，系统通过产物血缘和决策链生成归因分析：

1. **测试失败**：
   - 测试用例是否覆盖了需求中的场景？
   - 失败是否因为 Spec 中接口定义模糊？
   - 实现是否偏离了 Spec？

2. **验收不通过**：
   - 最终输出是否满足 `requirements.acceptance_criteria`？
   - 技术方案中是否遗漏了关键需求？
   - 用户是否在需求确认阶段没有提及某个场景？

**归因输出示例**：

```
验收失败根因分析：
- 直接原因：测试用例 test_range_validation 未通过
- 深层原因：Spec 中「范围校验」定义模糊，只写了支持 int，未说明是否支持 float
- 决策链：需求确认时用户未明确说明 float 场景（decision_id: xxx）
- 建议：回到 PLANNING_IMPLEMENTATION_SPECS 阶段，补充 float 范围校验规则
```

---

## 10. MVP 任务规格：config-validator CLI

### 10.1 需求

实现一个命令行工具 `config-validator`，用于校验 JSON 配置文件是否符合 schema 规则。

### 10.2 功能要求

- 读取 `config.json` 和 `schema.json`
- 校验规则：
  - 必填字段（`required`）
  - 字段类型（`string`, `integer`, `boolean`, `list`）
  - 数值范围（`minimum`, `maximum`，仅对 integer 生效）
- 输出格式：
  - 默认 text
  - `--format json` 输出 JSON
- 退出码：
  - 校验通过返回 0
  - 校验失败返回 1
- 命令示例：
  ```bash
  python -m config_validator config.json schema.json --format json
  ```

### 10.3 输入示例

`config.json`：
```json
{
  "name": "my-app",
  "port": 8080,
  "debug": false,
  "tags": ["web", "api"]
}
```

`schema.json`：
```json
{
  "required": ["name", "port"],
  "properties": {
    "name": {"type": "string"},
    "port": {"type": "integer", "minimum": 1, "maximum": 65535},
    "debug": {"type": "boolean"},
    "tags": {"type": "list"}
  }
}
```

### 10.4 预期产物

```
config-validator/
├── src/
│   └── config_validator/
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli.py
│       ├── schema.py
│       ├── validator.py
│       └── formatter.py
├── tests/
│   ├── test_validator.py
│   ├── test_schema.py
│   └── test_cli.py
├── README.md
└── pyproject.toml
```

### 10.5 验收标准

- 所有单元测试通过
- 覆盖正常、缺少必填字段、类型错误、范围越界等场景
- 手动运行命令验证通过
- 自动提交 Git commit

---

## 11. 实现计划（压缩为 3 个 Phase）

### Phase 1：核心骨架 + 单任务跑通（3-4 天）

目标：状态机、SQLite、`ClaudeCodeRuntime`、Git worktree 都能工作，先让 `feature` 任务 config-validator 在无飞书确认的情况下跑通。

任务：
1. 完成 `src/mac_system/` 骨架：`config`、`database`、`models`、`state_machine`。
2. 实现 `ProjectService`：创建项目、查询状态、记录决策。
3. 实现 `ClaudeCodeRuntime`：封装 `claude -p`，支持 `--output-format json`、`--json-schema`、`--allowedTools`、`--add-dir`。
4. 实现 `WorktreeManager`：创建、合并、清理 worktree。
5. 实现 `AgentOrchestrator`：根据状态调用 Agent、准备 `.mac/` 上下文、解析返回 JSON、更新数据库。
6. 为 `planner`（3 stages）、`coder`、`tester`、`reviewer` 编写 prompt 和 JSON schema。
7. 单元测试覆盖状态转换和 `ClaudeCodeRuntime` 命令拼装。
8. 用真实 `claude -p` 跑 config-validator，确认代码生成、测试、Review、合并到 feature 分支完整闭环。

### Phase 2：飞书人工确认闭环（2-3 天）

目标：把需求确认、方案确认、Spec 确认、验收确认接到飞书。

任务：
1. 实现 `FeishuBridge`：发送文本、确认卡片、冲突卡片、状态卡片，解析回调。
2. 实现 `project_users` 绑定：通过飞书 `open_id` 找到 project。
3. 在状态机进入 `*_CONFIRM_PENDING` 时自动发卡片。
4. 用户决策后调用 `/decisions` 推进/回退状态。
5. 支持追问：用户回复 → 调用 `project_manager` Agent 生成回答 → 更新卡片但不改状态。
6. 支持冲突裁决卡片，技术方案确认使用 `decisions` 预定义选项。
7. 本地跑通：从飞书创建项目 → 飞书确认 → 代码提交。

### Phase 3：补全任务类型与端到端验证（2-3 天）

目标：补 `bug_fix`、`documentation` Workflow Profile，跑通 3 个 MVP 任务。

任务：
1. 编写 `workflow_profiles/bug_fix.json` 和 `workflow_profiles/documentation.json`，复用 `delivery.json`。
2. 为 `bug_fix` 准备 planner stage=bug_analysis 的 prompt/schema。
3. 为 `documentation` 准备 planner stage=doc_outline 的 prompt/schema。
4. 跑通 Task 2（port 范围 0-65535）和 Task 3（`AGENTS.md`）。
5. 验证异常回退、暂停/恢复、冲突裁决。
6. 写操作文档和 README。

---

## 12. 技术栈确认

| 组件 | 选择 | 理由 |
|------|------|------|
| Agent 执行引擎 | **Claude Code CLI** | 每个 Specialist 是一个 `claude -p` 实例，能直接读写文件、跑测试 |
| Agent 编排 | **Python Orchestrator** | 自研状态机 + 任务调度，不依赖 CrewAI |
| LLM | Claude Code 自带（Anthropic API） | 通过 `claude` CLI 使用，也可配置第三方 provider |
| Web 框架 | **FastAPI** | 轻量，异步，便于暴露状态机 API |
| 数据库 | **SQLite** | MVP 够用，零配置，后续可迁移 |
| ORM | **SQLModel** | 和 Pydantic 配合好，简单直接 |
| 飞书 SDK | **lark-oapi** | 官方 Python SDK，稳定 |
| Git 操作 | **GitPython** + `git` CLI | Python 原生操作 Git |
| 测试 | **pytest** | Python 标准 |
| 集成测试 | **pytest + 真实 claude -p** | 验证端到端流程与产物 |

---

## 13. 风险与待决策事项

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| Claude Code CLI 调用开销高 | 中 | 每个 Agent 一次完整调用；MVP 任务小，可接受 |
| Claude Code 输出 JSON 不稳定 | 高 | 已定义各阶段输出 JSON schema，解析失败时重试 |
| Claude Code 在本地执行命令风险 | 高 | 沙箱目录内完全开放，沙箱外只读；所有操作审计 |
| 飞书卡片交互调试成本高 | 中 | 已定义卡片 JSON 结构；MVP 先文本消息验证，再升级为卡片 |
| 代码生成质量不可控 | 高 | Review Agent + 测试 + 人工验收三道防线 |
| 产物血缘记录遗漏 | 中 | 在 Task 层强制记录 input_data / output_data，并通过 event_logs 审计 |
| Git 自动提交风险 | 低 | MVP 只提交到 feature 分支，不自动 merge 到 main |
| 用户追问语义理解错误 | 中 | 追问不直接改状态，只更新上下文，最终决策仍由明确按钮触发 |
| Claude Code API 费用 | 中 | 真实运行消耗 API；MVP 任务小，且仅在开发/演示时触发 |

### 待用户决策

1. **Claude Code 凭证**：本地 `claude` CLI 是否已登录？是否有 Anthropic API Key？
2. **飞书应用**：是否已有企业飞书管理员权限创建自定义应用？
3. **MVP 是否先做文本交互？** 还是直接做卡片交互？

---

## 13.1 v3.2 多轮闭环补充设计

系统采用“执行态—输入态—确认态—返工路由”四类节点。执行态生成版本化产物；Agent 信息不足时进入该阶段输入态；确认态允许 confirm 或 revise；测试、Review 和验收的问题进入 REWORK，由失败分类推荐回退目标，用户做最终选择。

输入态是所有 Agent 的通用能力，而非只服务需求阶段。Requirements/Architecture/Specs Planner、Coder、Tester、Reviewer、Acceptance Evaluator、Project Manager 都可以在完成前发起结构化选项或自由文本问题，并连续多轮 grill-me。问题和回答持久化，携带 resume_state 和追踪 ID；blocking question 未解决时禁止 complete。各 Agent 的强制提问场景见 `specs/product_requirements.md`。

测试失败不默认全部交给 Coder：实现缺陷回 Coding，测试缺陷或环境问题回 Testing，契约不清回 Specs，方案问题回 Architecture，范围问题回 Requirements。Reviewer 的 blocking issues 必须作为下一轮 Coder 的输入，Coder 逐项输出 addressed_feedback，下一轮 Reviewer 逐项核验。

用户中途命令走控制通道并优先执行；Agent 运行期间的自由文本写入持久化 pending_user_inputs。Agent 返回后、merge/transition 前必须再次检查控制状态和输入队列。范围变更、纠正或 rollback 会让当前结果失效；普通问题可回答但不得静默改变已确认范围。

自动审批只适用于已经明确 pass 的质量门。runtime failure、quality fail、REWORK 和最终验收始终不能自动通过。

会话路由与项目状态机分层：conversation scope 负责确定 chat、actor、project 和权限；项目状态机只接收已经验证的 project event。切换 `/use` 只改变用户在当前聊天的 active project，不改变项目状态。群聊不同成员可以选择不同项目；项目状态变化提交一次后，再按 ProjectChat 订阅策略广播到多个会话。

设计参考：LangGraph 的持久化 interrupt/resume 用于可恢复的人类输入循环；SWE-agent RetryAgent 保留 attempt/review 供下一轮消费；GitHub Actions environment protection rules 将自动检查、等待和人工 reviewer 作为相互独立的 gate。MAC 不直接依赖这些实现，但采用相同的“持久化暂停、反馈进入下一轮、自动结果不替代人工保护规则”原则。

交付追踪采用 REQ → AC → DEC → SPEC → CODE/TEST → REV → Acceptance Matrix。上游确认版本改变时执行影响分析并使受影响下游产物 stale；状态回退不能代替依赖失效。最终验收必须逐 AC 提供实现、测试和 Review 证据。

- https://langchain-ai.github.io/langgraph/how-tos/human_in_the_loop/wait-user-input/
- https://swe-agent.com/latest/reference/agent/
- https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments

## 14. MVP 最终范围

### 14.1 三个真实任务

| 任务 | task_type | 目标 | 验收标准 |
|------|-----------|------|---------|
| **Task 1: config-validator CLI** | feature | 实现一个 JSON 配置校验器 CLI | 测试通过，命令可运行，代码提交到 feature 分支 |
| **Task 2: Port 范围 bug fix** | bug_fix | 修复 port 校验范围（改为 0-65535）| 新增测试覆盖边界，所有测试通过，代码提交 |
| **Task 3: AGENTS.md** | documentation | 为项目写一份 AI 助手行为守则 | 文档结构完整，符合项目需求，提交到 feature 分支 |

### 14.2 运行环境

- **Agent Runtime**：真实 Claude Code CLI（`claude -p`）
- **移动端**：真实飞书机器人
- **Git**：真实仓库提交（本地或远程 feature 分支）
- **数据库**：SQLite
- **后端**：Python + FastAPI

### 14.3 不需要 Mock

用户已确认有 API key 和飞书凭证，MVP 不使用 MockRuntime。系统直接调用：
- `claude -p` 执行 Agent
- `lark-oapi` 发送飞书卡片
- `git` 提交代码

因此所有实现、测试、演示均走真实 Runtime。测试用例应验证状态机流转、文件生成、Git 操作等可观测行为，而非替换 Claude Code。

### 14.4 成功标准

1. 从飞书发起任务，完整跑通 3 个任务的流程
2. 每个关键节点都能在手机上确认/驳回/追问
3. 异常时能正确回退到对应状态
4. 最终产物提交到 Git
5. 所有操作记录到 SQLite 审计日志

---

## 15. 下一步行动

建议按以下顺序启动：

1. **确认技术栈细节**：Claude Code 凭证、Git 目标仓库、飞书应用环境
2. **完成项目脚手架**：`pyproject.toml`、目录结构、`.env.example`
3. **Phase 1：核心骨架 + 单任务跑通**：状态机、SQLite、`ClaudeCodeRuntime`、worktree、config-validator feature
4. **Phase 2：飞书人工确认闭环**：绑定 open_id、发送/解析卡片、追问
5. **Phase 3：补全任务类型与端到端验证**：bug_fix、documentation、3 个任务全跑通

这个顺序可以确保：即使 Claude Code Agent 执行部分有问题，状态机和飞书通知层已经能独立验证。

---

*文档版本：v3.1*  
*创建时间：2026-07-07*  
*修订说明：
- 统一 Agent 角色为 planner / coder / tester / reviewer / acceptance_evaluator / project_manager
- 统一状态机状态名为 PLANNING_REQUIREMENTS / PLANNING_ARCHITECTURE / PLANNING_IMPLEMENTATION_SPECS / CODING / TESTING / REVIEW 等
- 删除所有 MockRuntime / mock-claude 内容，MVP 全程使用真实 Claude Code
- API/DB schema 与代码实现对齐
- 增加 `--add-dir` / `readonly_dirs` 沙箱外只读机制
- 增加飞书 open_id 到 project_id 的绑定设计
- 压缩实现计划为 3 个 Phase*
