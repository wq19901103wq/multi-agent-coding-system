# Agent Runtime 规范

> 版本：v2.0
> 用途：定义多 Provider Agent Runtime 的路由、调用、返回值和错误处理。

## Provider 与路由

生产入口统一通过 `build_agent_runtime()` 构造 `RuntimeRouter`，API、飞书 WebSocket
和 CLI 不得各自重复创建 Claude-only Runtime。路由来源是
`agent_config.json -> agents.<agent_type>.runtime`。

- `claude_code`：双向 stream-json，支持 system prompt file 和工具白名单。
- `kimi_code`：`--print --output-format stream-json` 无交互执行。
- `codex`：`codex exec --json` JSONL 执行；写角色使用 `workspace-write`，只读角色使用
  `read-only`。
- `zcode`：登记为 `desktop_only`；没有受支持的 headless 协议前不得调度。

Codex 不直接接收 MAC 的 Draft 7 output schema。当前 schema 使用 `allOf` 等合法构造，
而 Codex CLI 的 structured-output 子集会拒绝它们。完整 schema 已注入 prompt，最终对象
仍必须由 MAC 的 `validate_agent_output()` 做同一份确定性校验，不能降低合同强度。

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

所有 Provider 都必须归一化为 `AgentRuntimeResult`，并执行同一套本地 schema 校验、
canonical `.mac` 产物恢复、输出文件扫描、超时和跨 Runtime 实例取消。Provider 原始
JSONL 保存在日志中，用户进度只展示脱敏的工具摘要；不得展示 Write/Edit 正文或完整 diff。

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

### 当前能力边界（MVP 实际状态）

Runtime 启动 Claude Code CLI 时统一加 `--dangerously-skip-permissions`，**这意味着 CLI 内置的命令审批层被完全绕过**。MVP 阶段的安全边界实际由以下三层提供：

1. **工具白名单**：通过 `--tools` 和 `--allowedTools` 限定 Agent 可调用的工具集（如 `Read`、`Edit`、`Write`、`Bash`、`Glob`、`Grep`）。
2. **文件系统隔离**：Agent 在专用 worktree 中运行；`--add-dir` 仅授予沙箱外目录的只读访问。
3. **进程级隔离**：subprocess 在 worktree 作为 cwd 下启动，不继承 MAC System 的管理权限。

### 待实现的命令解析层

以下命令类别应在后续版本中通过独立的命令解析层拦截（当前未实现）：

- `rm -rf /` 或 `rm -rf ~`
- `sudo`
- `curl | sh` / `wget | sh`
- 修改 `/etc`、用户 home 配置等沙箱外文件
- 网络请求到外部 API（除已配置的 Git 远程）

> **重要**：在命令解析层落地之前，本节描述的拦截**不会发生**。任何依赖此保护层的 spec 或文档应明确标注为"待实现"。`--dangerously-skip-permissions` 与本节拦截清单同时出现在 spec 中是历史遗留，**不是描述两层独立保护**--前者使后者失效。

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
