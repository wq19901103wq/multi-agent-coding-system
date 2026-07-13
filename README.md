# Multi-Agent Coding System (MAC System)

一个条件人类闭环的多 Agent 编码系统，覆盖从需求分析到自动合并提交的完整软件交付流程。

## 核心特性

- **完整流程**：需求分析 → 技术规划 → Spec 拆分 → 代码实现 → 测试 → Review → 验收 → 自动 Git 合并
- **多轮人类闭环**：需求、方案、Spec 和质量门均可多轮澄清/修订，明确失败不会被自动放行
- **移动端审核**：通过飞书机器人接收确认卡片、追问、查看状态
- **反馈闭环**：测试与 Review 生成结构化反馈，按根因回退到需求、方案、Spec、编码或测试
- **中途控制**：Agent 运行期间的命令立即生效，自由文本持久化排队并在状态推进前消费
- **多会话路由**：群聊和私聊均可关联多个项目，当前项目按“用户 × 聊天”隔离，避免跨项目串话
- **全 Agent 主动澄清**：需求、架构、Spec、编码、测试、Review、验收和项目管理 Agent 均可通过选项或自由文本进行多轮 grill-me，回答后恢复原 Agent

## 快速开始

```bash
# 1. 安装依赖
pip install -e ".[dev]"

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 LLM 和飞书配置

# 3. 启动服务
python -m mac_system
```

## MVP 任务

第一个 end-to-end 任务：实现 `config-validator` CLI 工具。

详见 [DESIGN.md](DESIGN.md)。

## 项目结构

```
multi-agent-coding-system/
├── src/mac_system/       # 主程序
│   ├── api/              # FastAPI 接口
│   ├── agents/           # Claude Code Runtime、Prompt 与 Orchestrator
│   ├── api/              # Control API、Admin 与飞书 Webhook
│   ├── core/             # 项目、worktree 与锁管理
│   ├── feishu/           # 飞书长连接、卡片和消息处理
│   ├── models.py         # SQLModel 数据模型
│   └── state_machine.py  # 状态机加载与转换
├── specs/                # 可执行规范、JSON Schema 与审批模式
├── state_machines/       # feature / bug_fix / documentation 状态机
├── tests/                # 测试
├── projects/             # Agent 生成的项目工作区（每个子目录是独立 Git 仓库）
└── DESIGN.md             # 架构设计文档
```

## 状态与反馈原则

- 数据库是控制层真相源，`.mac/STATE.json` 是供 Agent 读取的可重建快照。
- Agent 主动提问进入输入态；用户要求修改回到同阶段新一轮；测试/Review/验收失败进入 `REWORK` 选择根因层级。
- night/auto 模式只可自动确认 Agent 已明确判定为 pass 的质量门；runtime failure、quality fail、REWORK 和最终验收不得自动通过。
- Reviewer 意见和 Tester 失败以 feedback packet 注入下一轮 Agent，Coder 必须逐项报告处理结果。
- 一个聊天可以关联多个项目；使用 `/use <project_id>` 切换当前上下文。卡片和显式命令始终按其 project_id/approval_id 路由，不受后来切换影响。
- 群聊只处理 @机器人、命令、回复机器人消息和卡片动作；没有唯一当前项目时必须先选择。

详细语义见 `specs/state_machine_semantics.md`、`specs/orchestrator_spec.md`、`specs/mac_protocol.md`、`specs/conversation_routing_spec.md` 和 `specs/artifact_traceability_spec.md`。
