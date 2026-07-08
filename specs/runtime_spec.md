# Agent Runtime 规范

> 版本：v1.0  
> 用途：定义 `ClaudeCodeRuntime` 的调用方式、参数、返回值、错误处理。

## 接口

```python
class AgentRuntime(ABC):
    @abstractmethod
    async def execute(
        self,
        agent_type: str,
        task_prompt: str,
        system_prompt: str,
        workspace: Path,
        worktree: Path,
        context_files: list[Path],
        output_schema: Path,
        allowed_tools: list[str],
        readonly_dirs: list[Path],
        timeout_seconds: int,
    ) -> AgentRuntimeResult:
        ...

@dataclass
class AgentRuntimeResult:
    success: bool
    output_json: dict | None
    output_files: list[Path]
    logs: str
    cost_info: dict | None
```

## Claude Code CLI 调用

### 命令模板

```bash
{claude_bin} -p "{task_prompt}" \
  --system-prompt-file {system_prompt_file} \
  --output-format json \
  --json-schema {output_schema} \
  --allowedTools "{allowed_tools}" \
  --dangerously-skip-permissions \
  {readonly_dir_args} \
  --workdir {worktree}
```

### 参数说明

| 参数 | 来源 | 说明 |
|------|------|------|
| `claude_bin` | `Settings.claude_bin` | Claude Code CLI 路径 |
| `-p` | user prompt 渲染结果 | 非交互模式 |
| `--system-prompt-file` | 临时文件 | 基础 system prompt + skills 合并 |
| `--output-format json` | 固定 | 要求输出 JSON |
| `--json-schema` | `output_schema` | 输出 schema 文件路径 |
| `--allowedTools` | `allowed_tools` | 逗号分隔的工具名 |
| `--dangerously-skip-permissions` | 固定 | 自动化必需 |
| `--add-dir` | `readonly_dirs` | 沙箱外只读目录，可多次使用 |
| `--workdir` | `worktree` | Agent 工作目录 |

### `readonly_dir_args` 格式

```bash
--add-dir /path/to/readonly/dir1 --add-dir /path/to/readonly/dir2
```

默认 `readonly_dirs` 只包含项目主工作区（让 Agent 能读取已有代码）。

## 返回值解析

### 成功

Claude Code 退出码为 0，stdout 为合法 JSON：

```python
result = AgentRuntimeResult(
    success=True,
    output_json=parsed_json,
    output_files=list_modified_files(worktree),
    logs=stderr,
    cost_info=extract_cost(stderr),
)
```

### 失败

| 场景 | 处理 | 是否重试 |
|------|------|---------|
| 退出码非 0 | `success=False`，`error_message=stderr` | 是 |
| stdout 不是合法 JSON | `json_parse_error` | 是 |
| JSON 不符合 schema | `schema_validation_error` | 是 |
| 执行超时 | `timeout` | 是 |
| Agent 输出 `success=false` | 按 `output.next_state_event` 处理 | 视事件而定 |

## 超时处理

- 默认超时：`AGENT_TIMEOUT_SECONDS`（默认 600 秒）
- 超时后发送 `SIGTERM`，5 秒后若未退出则发送 `SIGKILL`
- 超时视为失败，计入 `MAX_AGENT_RETRIES`

## 成本追踪

从 Claude Code stderr 中解析 token 使用量（若可用）：

```json
{
  "input_tokens": 1234,
  "output_tokens": 567,
  "total_tokens": 1801,
  "estimated_cost_usd": 0.0054
}
```

若无法解析，则 `cost_info=None`。

## 安全限制

### Bash 命令白名单（默认）

允许：
- `python` / `python3`
- `pytest`
- `git`
- `ls` / `cat` / `find` / `grep`
- `mkdir` / `touch`
- `pip` / `uv`（仅安装沙箱内依赖）

禁止（Agent 尝试执行时 Orchestrator 应拦截）：
- `rm -rf /` 或 `rm -rf ~`
- `sudo`
- `curl | sh` / `wget | sh`
- 修改 `/etc`、用户 home 配置等沙箱外文件
- 网络请求到外部 API（除已配置的 Git 远程）

> 说明：MVP 阶段主要依赖 Claude Code `--allowedTools` 限制，后续可增加命令解析层。

## 日志记录

每次执行记录到 `event_logs`：

```json
{
  "level": "INFO",
  "message": "Agent executed",
  "meta": {
    "agent_type": "coder",
    "task_id": "task_001",
    "exit_code": 0,
    "duration_seconds": 45,
    "cost_info": {...}
  }
}
```
