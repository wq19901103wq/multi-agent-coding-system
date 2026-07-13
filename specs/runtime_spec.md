# Agent Runtime 规范

> 版本：v1.1
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

### 执行模型

Runtime 采用 **Claude Code CLI 托管工具执行**：

```text
模型产生 tool_use
  -> Claude Code CLI 根据 allowedTools / permission mode 执行工具
  -> CLI 通过 replay user message 输出真实 tool_result
  -> MAC Runtime 只观察并转发进度
  -> CLI 输出最终 result 事件
```

职责边界：

- Claude Code CLI 是 Read/Write/Edit/Bash/Glob/Grep 的唯一执行者。
- Runtime 收到 `assistant.content[].type == "tool_use"` 时只能记录/推送进度，不得本地执行工具，不得向 stdin 回写 `tool_result`。
- Runtime 收到 replay 的 `user.tool_result` 时只做审计和展示；同一 `tool_use_id` 的 replay 必须幂等去重。
- Runtime 可通过 `--allowedTools`、permission mode、worktree 和进程隔离限制能力，但权限决策不等于工具执行。
- stream-json 单行可能包含完整 Write/Edit 内容；subprocess `StreamReader` 上限不得使用 asyncio 默认约 64 KiB，当前下限为 16 MiB。reader 异常必须通过队列显式传播，并在 `finally` 中发送关闭 sentinel，禁止静默等待到 Agent 超时。
- 工具进度事件不得转发完整 Write/Edit 内容，只发送文件路径、内容长度等摘要。

禁止同时启用 CLI 内置执行和 Runtime 本地 `_execute_tool()`。否则同一 Write/Edit 会发生双写，第二轮修改已有产物时可能触发 `File has been modified since read` 并使会话无法产生最终 result。

### 命令模板

```bash
{claude_bin} \
  --input-format stream-json \
  --output-format stream-json \
  --system-prompt-file {system_prompt_file} \
  --allowedTools "{allowed_tools}" \
  --replay-user-messages \
  --dangerously-skip-permissions \
  {readonly_dir_args}
```

### 参数说明

| 参数 | 来源 | 说明 |
|------|------|------|
| `claude_bin` | `Settings.claude_bin` | Claude Code CLI 路径 |
| stdin user message | user prompt 渲染结果 | stream-json 会话启动后发送 |
| `--system-prompt-file` | 临时文件 | 基础 system prompt + skills 合并 |
| `--input-format stream-json` | 固定 | 双向会话输入 |
| `--output-format stream-json` | 固定 | 输出 assistant/tool/result 事件 |
| `--replay-user-messages` | 固定 | 观察 CLI 产生的真实 tool result |
| `--allowedTools` | `allowed_tools` | 逗号分隔的工具名 |
| `--dangerously-skip-permissions` | 固定 | 自动化必需 |
| `--add-dir` | `readonly_dirs` | 沙箱外只读目录，可多次使用 |
| subprocess cwd | `worktree` | Agent 工作目录 |

### `readonly_dir_args` 格式

```bash
--add-dir /path/to/readonly/dir1 --add-dir /path/to/readonly/dir2
```

默认 `readonly_dirs` 只包含项目主工作区（让 Agent 能读取已有代码）。

## 返回值解析

### 成功

收到 `type=result` 且 `is_error=false`，并能从 `result` 解析任务元信息 JSON：

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
| 执行超时且无可恢复产物 | `timeout` | 是 |
| Agent 输出 `success=false` | 按 `output.next_state_event` 处理 | 视事件而定 |

## 超时处理

- 默认超时：`AGENT_TIMEOUT_SECONDS`（默认 600 秒）
- 超时后发送 `SIGTERM`，5 秒后若未退出则发送 `SIGKILL`
- 超时视为失败，计入 `MAX_AGENT_RETRIES`
- 若 CLI 未产生最终 `result`，Runtime 返回明确的 `No result event received before timeout` 摘要，不应把完整 stream-json 填入任务错误字段；完整事件写入独立日志。
- Orchestrator 可以在目标产物存在、JSON 可解析且 schema 校验通过时恢复任务结果；不得仅凭文件存在判定成功。

## 服务重启恢复

- 启动时将遗留的 `running` task 标记为 `failed`，错误原因为 `service restarted while task was running`。
- 按 `project_id` 去重；仅对 `status=active` 且当前状态类型为 `agent_execution` 的项目重新调度一次 tick。
- 重启恢复沿用项目级 `retry_counts` 和并发锁，不得同时启动两个相同 project/state 的任务。
- human confirmation/input、paused、cancelled、failed、completed 项目不自动启动 Agent。

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
