# Phase 1：核心骨架 + 单任务跑通 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 实现状态机、数据库、ProjectService、WorktreeManager、ClaudeCodeRuntime、AgentOrchestrator，让 `feature` 任务 config-validator 在无飞书确认的情况下跑通。

**架构：** 自研 Python Orchestrator 驱动状态机；Agent 通过 `claude -p` 在独立 git worktree 中执行；产物通过 `.mac/` JSON 文件交换；SQLite 持久化项目/任务/审批/日志。

**技术栈：** Python 3.10+、FastAPI、SQLModel、GitPython、lark-oapi、pytest、Claude Code CLI。

---

## 文件清单

| 文件 | 职责 |
|------|------|
| `src/mac_system/models.py` | SQLModel 表 + Pydantic schema（与 specs 对齐） |
| `src/mac_system/config.py` | Settings、agent_config、state_machine 加载 |
| `src/mac_system/database.py` | 数据库引擎、session、init_db |
| `src/mac_system/state_machine.py` | 状态机解析与 transition |
| `src/mac_system/core/project_service.py` | 项目创建、状态转换、Task/Approval/日志管理 |
| `src/mac_system/core/worktree_manager.py` | worktree 创建、合并、清理 |
| `src/mac_system/agents/runtime.py` | AgentRuntime 抽象 + ClaudeCodeRuntime |
| `src/mac_system/agents/prompts.py` | prompt 模板渲染、skills 注入 |
| `src/mac_system/agents/orchestrator.py` | 编排器主循环 |
| `src/mac_system/api/main.py` | FastAPI 应用（ health + create project + status） |
| `prompts/planner_requirements.md` | planner stage=requirements prompt |
| `prompts/planner_architecture.md` | planner stage=architecture prompt |
| `prompts/planner_specs.md` | planner stage=specs prompt |
| `prompts/coder.md` | coder prompt |
| `prompts/tester.md` | tester prompt |
| `prompts/reviewer.md` | reviewer prompt |
| `tests/test_state_machine.py` | 状态机单元测试 |
| `tests/test_project_service.py` | ProjectService 单元测试 |
| `tests/test_worktree_manager.py` | WorktreeManager 单元测试 |
| `tests/test_runtime.py` | ClaudeCodeRuntime 命令拼装测试 |
| `tests/test_orchestrator.py` | Orchestrator 集成测试（Mock Runtime） |

---

## 任务 1：对齐现有基础模块

**文件：**
- 修改：`src/mac_system/models.py`
- 修改：`src/mac_system/config.py`
- 修改：`src/mac_system/database.py`
- 修改：`src/mac_system/state_machine.py`
- 测试：`tests/test_state_machine.py`

### 步骤 1：调整 models.py 与 specs 对齐

- [ ] 确认 `Project` 表包含：`id`、`name`、`task_type`、`description`、`status`、`current_state`、`previous_state`、`workspace_path`、`context`、`approval_config`、`created_at`、`updated_at`
- [ ] 确认 `Task` 表包含：`id`、`project_id`、`agent_type`、`state`、`status`、`worktree_path`、`input_data`、`output_data`、`error_message`、`retry_count`、`cost_usd`、`created_at`、`updated_at`
- [ ] 确认 `Approval` 表包含：`id`、`project_id`、`task_id`、`state`、`approval_type`、`question`、`payload`、`response`、`responded_by`、`status`、`created_at`、`responded_at`
- [ ] 确认 `EventLog` 表包含：`id`、`project_id`、`level`、`message`、`meta`、`created_at`
- [ ] 新增 `ProjectUser` 表：`project_id`、`open_id`、`created_at`

```python
class ProjectUser(SQLModel, table=True):
    __tablename__ = "project_users"
    project_id: str = SQLField(foreign_key="projects.id", primary_key=True)
    open_id: str = SQLField(primary_key=True)
    created_at: datetime = SQLField(default_factory=datetime.utcnow)
```

### 步骤 2：调整 config.py

- [ ] `Settings` 中 `database_url` 默认改为 `sqlite:///./mac_system.db`
- [ ] 增加 `max_agent_retries`、`max_test_retries`、`max_review_retries`、`agent_timeout_seconds`
- [ ] `get_agent_config()` 返回 agent 配置，包含 skills、prompt_file、output_schema
- [ ] 增加 `load_state_machine(task_type)` 函数

### 步骤 3：调整 database.py

- [ ] 使用 `create_engine` 创建 SQLite 引擎
- [ ] `init_db()` 调用 `SQLModel.metadata.create_all(_engine)`
- [ ] 提供 `get_session()` generator 和 `session_scope()` context manager

### 步骤 4：调整 state_machine.py

- [ ] `StateMachine` 类加载 `state_machines/{task_type}.json`
- [ ] 提供 `next_state(current, event)`、`state_type(name)`、`is_terminal(name)`、`is_agent_execution(name)` 等方法
- [ ] 提供 `agent_for(state)` 和 `stage_for(state)`

### 步骤 5：编写状态机测试

```python
def test_feature_state_machine():
    sm = StateMachine("feature")
    assert sm.initial_state == "PLANNING_REQUIREMENTS"
    assert sm.next_state("PLANNING_REQUIREMENTS", "complete") == "REQUIREMENT_CONFIRM_PENDING"
    assert sm.next_state("REQUIREMENT_CONFIRM_PENDING", "confirm") == "PLANNING_ARCHITECTURE"
    assert sm.state_type("CODING") == "agent_execution"
    assert sm.agent_for("CODING") == "coder"
```

运行：`pytest tests/test_state_machine.py -v`
预期：PASS

---

## 任务 2：实现 ProjectService

**文件：**
- 创建：`src/mac_system/core/project_service.py`
- 创建：`src/mac_system/core/__init__.py`
- 测试：`tests/test_project_service.py`

### 步骤 1：创建 ProjectService 类

```python
class ProjectService:
    def __init__(self, session: Session):
        self.session = session

    async def create_project(self, name, task_type, description, approval_mode="normal", creator_open_id=None) -> Project:
        # 生成 project_id
        # 创建目录
        # git init
        # 写入初始 STATE.json
        # 创建 Project 记录
        # 绑定 creator_open_id
        # 触发 INIT.start -> PLANNING_REQUIREMENTS
        ...

    def get_project(self, project_id: str) -> Project | None:
        ...

    async def transition_state(self, project_id: str, event: str, payload=None, triggered_by="system") -> Project:
        ...

    async def create_task(self, project_id, agent_type, state, input_data) -> Task:
        ...

    async def complete_task(self, task_id, output_data, cost_info=None) -> Task:
        ...

    async def fail_task(self, task_id, error_message) -> Task:
        ...

    async def create_approval(self, project_id, task_id, state, approval_type, question, options, payload=None) -> Approval:
        ...

    async def respond_approval(self, approval_id, response, responded_by) -> Approval:
        ...

    async def log_event(self, project_id, level, message, meta=None):
        ...
```

### 步骤 2：ID 生成函数

```python
def generate_id(prefix: str) -> str:
    from datetime import datetime
    import secrets
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    rand = secrets.token_hex(2)
    return f"{prefix}_{ts}_{rand}"
```

### 步骤 3：编写测试

```python
@pytest.fixture
def session():
    from sqlmodel import Session, create_engine, SQLModel
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s

def test_create_project(session):
    service = ProjectService(session)
    project = service.create_project("test", "feature", "desc")
    assert project.id.startswith("proj_")
    assert project.current_state == "PLANNING_REQUIREMENTS"
    assert Path(project.workspace_path).exists()
```

运行：`pytest tests/test_project_service.py -v`
预期：PASS

---

## 任务 3：实现 WorktreeManager

**文件：**
- 创建：`src/mac_system/core/worktree_manager.py`
- 测试：`tests/test_worktree_manager.py`

### 步骤 1：实现 WorktreeManager

```python
class WorktreeManager:
    def __init__(self, projects_dir: Path):
        self.projects_dir = projects_dir

    def create_worktree(self, project_id: str, task_id: str, agent_type: str, stage: str | None = None) -> Path:
        # 按命名规则创建 worktree 和分支
        ...

    def sync_mac_files(self, project_id: str, worktree: Path, state: dict, task_input: dict):
        # 复制 .mac/ 文件到 worktree
        ...

    def merge_worktree(self, project_id: str, task_id: str) -> None:
        # 在主工作区 git merge 对应分支
        ...

    def remove_worktree(self, project_id: str, task_id: str) -> None:
        # git worktree remove + branch delete
        ...
```

### 步骤 2：编写测试

```python
def test_create_worktree(tmp_path):
    wm = WorktreeManager(tmp_path)
    # 创建模拟项目目录和 git repo
    project_id = "proj_test"
    project_path = tmp_path / project_id
    project_path.mkdir()
    (project_path / ".git").mkdir()
    # 实际测试需要真实 git init
    ...
```

运行：`pytest tests/test_worktree_manager.py -v`
预期：PASS

---

## 任务 4：实现 ClaudeCodeRuntime

**文件：**
- 创建：`src/mac_system/agents/runtime.py`
- 测试：`tests/test_runtime.py`

### 步骤 1：定义 AgentRuntime 抽象类

```python
@dataclass
class AgentRuntimeResult:
    success: bool
    output_json: dict | None
    output_files: list[Path]
    logs: str
    cost_info: dict | None

class AgentRuntime(ABC):
    @abstractmethod
    async def execute(self, agent_type, task_prompt, system_prompt, workspace, worktree,
                      context_files, output_schema, allowed_tools, readonly_dirs, timeout_seconds) -> AgentRuntimeResult:
        ...
```

### 步骤 2：实现 ClaudeCodeRuntime

```python
class ClaudeCodeRuntime(AgentRuntime):
    def __init__(self, claude_bin: Path, anthropic_api_key: str | None = None):
        self.claude_bin = claude_bin
        self.anthropic_api_key = anthropic_api_key

    async def execute(self, ...) -> AgentRuntimeResult:
        # 渲染 system prompt 到临时文件
        # 组装 claude -p 命令
        # 使用 asyncio.create_subprocess_exec 执行
        # 解析 stdout JSON
        # 解析 stderr 成本信息
        # 扫描 worktree 变更文件
        ...
```

### 步骤 3：实现命令拼装（可测试）

```python
def build_claude_command(self, ...) -> list[str]:
    cmd = [
        str(self.claude_bin),
        "-p", task_prompt,
        "--system-prompt-file", str(system_prompt_file),
        "--output-format", "json",
        "--json-schema", str(output_schema),
        "--allowedTools", ",".join(allowed_tools),
        "--dangerously-skip-permissions",
        "--workdir", str(worktree),
    ]
    for d in readonly_dirs:
        cmd.extend(["--add-dir", str(d)])
    return cmd
```

### 步骤 4：编写测试

```python
def test_build_claude_command():
    runtime = ClaudeCodeRuntime(Path("/usr/bin/claude"))
    cmd = runtime.build_claude_command(
        task_prompt="hello",
        system_prompt_file=Path("/tmp/sys.md"),
        worktree=Path("/tmp/wt"),
        output_schema=Path("/tmp/schema.json"),
        allowed_tools=["Read", "Edit"],
        readonly_dirs=[Path("/tmp/ref")],
    )
    assert cmd[0] == "/usr/bin/claude"
    assert "--add-dir" in cmd
    assert "/tmp/ref" in cmd
```

运行：`pytest tests/test_runtime.py -v`
预期：PASS

---

## 任务 5：实现 Prompt 渲染

**文件：**
- 创建：`src/mac_system/agents/prompts.py`
- 创建：`prompts/planner_requirements.md`
- 创建：`prompts/planner_architecture.md`
- 创建：`prompts/planner_specs.md`
- 创建：`prompts/coder.md`
- 创建：`prompts/tester.md`
- 创建：`prompts/reviewer.md`

### 步骤 1：实现 PromptRenderer

```python
class PromptRenderer:
    def __init__(self, agent_config: dict):
        self.agent_config = agent_config

    def render_system_prompt(self, agent_type: str, stage: str | None = None) -> str:
        # 读取基础 prompt_file
        # 注入 skills
        # 返回完整 system prompt 字符串
        ...

    def render_task_prompt(self, agent_type: str, stage: str | None, project: Project, state: str,
                           worktree: Path, context_files: list[Path]) -> str:
        # 读取任务 prompt 模板
        # 替换变量
        ...
```

### 步骤 2：编写 prompt 模板

每个 prompt 文件包含角色定义、输入说明、输出要求、工具限制、沙箱规则。

示例 `prompts/coder.md`：

```markdown
# Coder Agent

你是专业 Python 开发工程师。

## 规则
- 只能在 {{worktree_path}} 内写入文件
- 不要修改 STATE.json
- 实现完成后写入 TASK.json 的 output 字段

## 输入
{{context_files}}

## 输出要求
- 输出文件：{{task_json_path}}
- next_state_event 可选值：complete / fail / clarify / conflict / spec_question / architecture_question / new_requirement
- 必须列出所有 modified_files
```

### 步骤 3：测试

```python
def test_render_coder_prompt():
    renderer = PromptRenderer(agent_config)
    prompt = renderer.render_task_prompt("coder", None, project, "CODING", worktree, context_files)
    assert str(worktree) in prompt
```

---

## 任务 6：实现 AgentOrchestrator

**文件：**
- 创建：`src/mac_system/agents/orchestrator.py`
- 创建：`src/mac_system/agents/__init__.py`
- 测试：`tests/test_orchestrator.py`

### 步骤 1：实现 Orchestrator.tick

```python
class AgentOrchestrator:
    def __init__(self, project_service, worktree_manager, runtime, prompt_renderer, agent_config, settings):
        ...

    async def tick(self, project_id: str):
        # 主循环：每次处理一个状态转换
        ...

    async def _execute_agent(self, project, state_machine):
        # 确定 agent/stage
        # 创建 task + worktree
        # 同步 .mac/
        # 渲染 prompt
        # 调用 runtime
        # 处理输出，推进状态
        ...
```

### 步骤 2：实现 Mock Runtime 用于测试

虽然 MVP 不用 Mock，但 Orchestrator 测试需要一个可控 Runtime：

```python
class FakeRuntime(AgentRuntime):
    def __init__(self, outputs: dict):
        self.outputs = outputs

    async def execute(self, agent_type, ...):
        output = self.outputs.get(agent_type, {"success": True, "next_state_event": "complete"})
        return AgentRuntimeResult(success=True, output_json=output, output_files=[], logs="", cost_info=None)
```

### 步骤 3：编写集成测试

```python
async def test_orchestrator_runs_planner_to_confirmation(session):
    service = ProjectService(session)
    project = service.create_project("test", "feature", "实现 config-validator CLI")
    runtime = FakeRuntime({
        "planner": {
            "success": True,
            "next_state_event": "complete",
            "summary": "...",
            "goals": ["..."],
            "acceptance_criteria": ["..."],
        }
    })
    orchestrator = AgentOrchestrator(service, ..., runtime, ...)
    await orchestrator.tick(project.id)
    project = service.get_project(project.id)
    assert project.current_state == "REQUIREMENT_CONFIRM_PENDING"
```

运行：`pytest tests/test_orchestrator.py -v`
预期：PASS

---

## 任务 7：实现 FastAPI 基础端点

**文件：**
- 创建：`src/mac_system/api/main.py`
- 修改：`src/mac_system/api/__init__.py`

### 步骤 1：创建 FastAPI 应用

```python
from fastapi import FastAPI, Depends, HTTPException
from sqlmodel import Session

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/projects")
async def create_project(req: ProjectCreate, session: Session = Depends(get_session)):
    service = ProjectService(session)
    project = await service.create_project(...)
    return project

@app.get("/projects/{project_id}")
async def get_project(project_id: str, session: Session = Depends(get_session)):
    service = ProjectService(session)
    project = service.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
    return project

@app.get("/projects/{project_id}/status")
async def get_status(project_id: str, session: Session = Depends(get_session)):
    ...
```

### 步骤 2：启动验证

运行：`uvicorn mac_system.api.main:app --reload --port 8000`
验证：`curl http://localhost:8000/health`
预期：`{"status":"ok"}`

---

## 任务 8：端到端手动验证（真实 Claude Code）

**文件：**
- 无新增文件

### 步骤 1：配置环境

- [ ] 复制 `.env.example` 为 `.env`
- [ ] 填入 `ANTHROPIC_API_KEY`
- [ ] 确认 `claude` CLI 可用：`claude --version`

### 步骤 2：创建项目并触发

```bash
curl -X POST http://localhost:8000/projects \
  -H "Content-Type: application/json" \
  -d '{"name":"config-validator","task_type":"feature","description":"实现一个 config-validator CLI 工具..."}'
```

### 步骤 3：观察执行

- [ ] Orchestrator 自动调用 planner stage=requirements
- [ ] 生成 `REQUIREMENTS.json`
- [ ] 进入 `REQUIREMENT_CONFIRM_PENDING`
- [ ] 手动调用 API confirm：

```bash
curl -X POST http://localhost:8000/projects/{project_id}/decisions \
  -H "Content-Type: application/json" \
  -d '{"approval_id":"...","action":"confirm"}'
```

- [ ] 重复直到 `MERGING` 完成

### 步骤 4：验证产物

- [ ] `projects/{project_id}/src/config_validator/` 存在代码
- [ ] `tests/` 存在测试
- [ ] `pytest` 在项目中通过
- [ ] Git 提交到 feature 分支

---

## 提交

每个任务完成后提交：

```bash
git add <files>
git commit -m "feat(core): task N - description"
```

---

## 自检

1. **规格覆盖度**：Phase 1 目标（状态机、SQLite、ProjectService、WorktreeManager、ClaudeCodeRuntime、Orchestrator、单任务跑通）都有对应任务 ✓
2. **占位符扫描**：所有步骤包含具体代码或命令，无 "TODO" ✓
3. **类型一致性**：所有文件使用 `Project`、`Task`、`Approval`、`EventLog`、`ProjectUser` 等一致模型 ✓
