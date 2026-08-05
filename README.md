# MAC System

MAC 把 Codex 的完整 coding-agent 和原生多 Agent 能力带到飞书，并支持项目目录绑定、移动端
卡片、多 coding-agent 后端、持久会话、计费和本地服务托管。

## 核心行为

- 每个项目对应一个持久 Codex Root Agent Thread；
- Root Agent 第一轮即可直接读写、运行和调试项目；
- 多 Agent 使用 Codex 原生 `spawn_agent`、消息、等待和打断工具；
- 用户运行中补充内容使用 `turn/steer`，idle 后继续同一 thread；
- 原生 `request_user_input` 显示为飞书选择/输入卡，并在原 turn 中继续；
- 一轮结束后项目保持可迭代，支持新建、修改、debug 和已有目录继续开发；
- Claude Code、Kimi 和 Pi 通过额外后端适配器供 Root Agent 按需调用。

MAC 不包含固定的需求→架构→编码→测试状态机，也没有额外 Coordinator Agent。

## 启动

```bash
pip install -e ".[dev]"
cp .env.example .env
./scripts/start_server.sh
```

验证服务：

```bash
lsof -Pi :18080 -sTCP:LISTEN
curl http://127.0.0.1:18080/admin/projects
```

验证/生产禁止使用不受限的 `uvicorn --reload`，项目文件变化会反复重启服务。

## 常用飞书命令

- `/create` 或 `/c`：创建系统管理目录中的新项目；
- `/open` 或 `/o`：打开已有 Git 项目（兼容 `/bind`、`/b`）；
- `/proj` 或 `/ls`：查看项目；
- `/use` 或 `/s`：切换项目；
- `/info` 或 `/i`：查看当前项目、真实工作目录和状态；
- `/new` 或 `/n`：保留项目和目录，开始新的 Codex 对话；
- `/pause`、`/resume`、`/stop`：暂停、恢复或终止当前项目；
- `/menu` 或 `/m`：用卡片完成常用操作；
- `/help` 或 `/h`：查看全部快捷命令。

正常项目工作直接使用自然语言，不要求用户选择任务类型、研发阶段或 coding-agent。系统输出使用
卡片；必须询问用户时优先提供选项，同时保留自由输入。

## 代码结构

```text
src/mac_system/
├── agents/root_runtime.py   # Codex app-server 薄客户端
├── core/root_agent.py       # 项目 Root Thread 与飞书投影
├── core/root_sessions.py    # Root identity / dynamic tool 幂等
├── agents/runtime.py        # 外部 coding-agent 后端
├── feishu/                  # WebSocket、消息和卡片
├── core/project_service.py  # 项目与持久化服务
└── api/                     # FastAPI 与管理页
```

架构见 [docs/architecture.md](docs/architecture.md)，设计原则见
[docs/mac-design-principles.md](docs/mac-design-principles.md)，飞书交互的可验收行为见
[docs/interaction-spec.md](docs/interaction-spec.md)。测试报告必须使用
[docs/testing-taxonomy.md](docs/testing-taxonomy.md) 中的 T1–T9 术语。
