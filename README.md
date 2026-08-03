# Multi-Agent Coding System (MAC System)

一个条件人类闭环的多 Agent 编码系统，覆盖从需求分析到自动合并提交的完整软件交付流程。

## 核心特性

- **完整流程**：Specify（需求 + 产品行为 + 验收标准）→ Design（技术方案）→ Plan（实现计划与任务拆分）→ 代码实现 → 测试 → Review → 验收 → 自动 Git 合并
- **多轮人类闭环**：Specify、Design、Plan 和质量门均可多轮澄清/修订，明确失败不会被自动放行
- **移动端审核**：通过飞书机器人接收确认卡片、追问、查看状态
- **反馈闭环**：测试与 Review 生成结构化反馈，按根因回退到 Specify、Design、Plan、编码或测试
- **中途控制**：Agent 运行期间的命令立即生效，自由文本持久化排队并在状态推进前消费
- **多会话路由**：群聊和私聊均可关联多个项目，当前项目按“用户 × 聊天”隔离，避免跨项目串话
- **业务 Agent 主动澄清**：Specify、架构、实现 Spec、编码、测试、Review 和验收评估均可进入统一问题交互并在回答后恢复原阶段；Project Manager 控制辅助仍通过协调层澄清
- **统一协调与有界上下文**：Coordinator 是唯一用户对话入口，业务 Agent 只接收带版本的 WorkItem 相关事实，澄清先回到 Coordinator，事实不足才询问用户
- **可靠通知**：流程关键卡片通过持久化 Outbox 去重、记录发送结果并支持中断恢复
- **可恢复运行失败**：技术重试自动执行；重试耗尽后进入恢复卡，可再次重试、选择回退阶段或终止，不与质量失败混淆

## 快速开始

```bash
# 1. 安装依赖
pip install -e ".[dev]"

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 LLM 和飞书配置
# 同时设置 CONTROL_API_TOKEN；如需绑定现有仓库，设置 MAC_ALLOWED_BIND_ROOTS

# 3. 启动服务
python -m mac_system
```

Control API 使用 `Authorization: Bearer <CONTROL_API_TOKEN>`。未配置 token
时 `/projects` 与 `/test` 接口会拒绝请求；`/bind` 和 API 的
`bound_directory` 只能指向 `MAC_ALLOWED_BIND_ROOTS` 内的目录。

## MVP 任务

第一个 end-to-end 任务：实现 `config-validator` CLI 工具。

详见 [DESIGN.md](DESIGN.md)。

新 Agent 开始工作前请先阅读 [Agent 入职与工作交接](docs/agent_onboarding_handoff.md)。
当前组件边界、数据契约和关键不变量见 [系统架构](docs/architecture.md)。
所有测试结论必须遵循 [测试与黑盒验证术语规范](docs/testing-taxonomy.md)，尤其不能把真实飞书群中的 Simulator E2E 当作真实私聊 UI 黑盒。

## 项目结构

```
multi-agent-coding-system/
├── src/mac_system/       # 主程序
│   ├── api/              # FastAPI 接口
│   ├── agents/           # 多 Provider Runtime、Prompt 与 Orchestrator
│   ├── api/              # Control API、Admin 与飞书 Webhook
│   ├── core/             # 项目、worktree 与锁管理
│   ├── feishu/           # 飞书长连接、卡片和消息处理
│   ├── models.py         # SQLModel 数据模型
│   └── state_machine.py  # 状态机加载与转换
├── specs/                # 可执行规范、JSON Schema 与审批模式
├── state_machines/       # 唯一的通用交付状态机模板
├── workflow_profiles/    # feature / iteration / bug_fix / documentation 阶段配置
├── tests/                # 测试
├── projects/             # Agent 生成的项目工作区（每个子目录是独立 Git 仓库）
└── DESIGN.md             # 架构设计文档
```

## 状态与反馈原则

- 数据库是控制层真相源，`.mac/STATE.json` 是供 Agent 读取的可重建快照。
- Agent 主动提问进入输入态；用户要求修改回到同阶段新一轮；测试/Review/验收失败进入 `REWORK` 选择根因层级。
- night/auto 模式只可自动确认 Agent 已明确判定为 pass 的质量门；runtime failure、quality fail 和 REWORK 不得自动通过。最终阶段由 Codex 主动从真实入口做黑盒验收、Claude 独立复核；normal/night/auto 在证据完整时自动交付，strict 保留人工终审。只有偏好、授权或系统无法获取的外部事实可以中断用户。
- Reviewer 意见和 Tester 失败以 feedback packet 注入下一轮 Agent，Coder 必须逐项报告处理结果。
- 一个聊天可以关联多个项目；使用 `/use <project_id>` 切换当前上下文。卡片和显式命令始终按其 project_id/approval_id 路由，不受后来切换影响。
- 群聊只处理 @机器人、命令、回复机器人消息和卡片动作；没有唯一当前项目时必须先选择。

当前真实状态见 `CURRENT_STATE.md`，后续工作见 `ROADMAP.md`。详细语义见 `specs/state_machine_semantics.md`、`specs/orchestrator_spec.md`、`specs/mac_protocol.md`、`specs/conversation_routing_spec.md`、`specs/conversation_router_spec.md` 和 `specs/artifact_traceability_spec.md`。
