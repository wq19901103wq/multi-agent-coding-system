# 多 Agent 编码系统调研报告

> 调研时间：2026-07-07  
> 调研范围：GitHub 开源项目、Claude Code 生态、移动端审批方案

---

## 一、直接相关项目

### 1. Claude Code Agent Teams（官方实验功能）

- **链接**：Claude Code CLI v2.1.32+
- **核心能力**：一个 team lead 协调多个 teammate，teammate 可直接互相通信
- **局限性**：交互式、同步，不能异步飞书触发
- **借鉴**：多个 Claude Code 实例协作可行，但需要外部 orchestrator 做异步调度

### 2. Overstory（已归档）→ Warren

- **链接**：https://github.com/jayminwest/overstory
- **核心能力**：
  - 每个 Agent 在独立 git worktree 中运行
  - 通过 SQLite mail system 协调
  - 支持 11 种 runtime：Claude Code、Pi、Gemini CLI、Aider、Goose 等
  - 分层冲突解决
- **借鉴**：
  - ✅ git worktree 隔离避免 Agent 冲突
  - ✅ Pluggable AgentRuntime 接口
  - ✅ SQLite 作为协调中枢

### 3. oh-my-claudecode / oh-my-team

- **核心能力**：5 个并发 Claude Code worker，独立 git worktree，Ultrapilot 并行模式
- **借鉴**：
  - ✅ 并行执行需要任务分解和结果合并
  - ✅ Review gate 是质量控制关键
  - ✅ 支持 Telegram/Slack Hub 远程查看

### 4. Claude MPM (claude-mpm)

- **链接**：https://github.com/bobmatnyc/claude-mpm
- **核心能力**：围绕 Claude Code CLI 的项目管理框架，skills、MCP、session 管理
- **借鉴**：
  - ✅ 把 Claude Code 当作执行引擎，上层做项目/工作流管理

### 5. wanman

- **核心能力**：worktree-isolated multi-agent runtime，JSON-RPC subprocess 控制
- **借鉴**：
  - ✅ subprocess + JSON-RPC 控制 Claude Code 可行
  - ✅ 任务池和产物存储需要统一管理

### 6. Agent Vault

- **链接**：https://github.com/loonshoot/agent_vault
- **核心能力**：MCP server，给 AI Agent 的敏感操作加 mobile approval
- **借鉴**：
  - ✅ 手机审批模式已被验证
  - ✅ SQLite 审计日志
  - ✅ 审批链接比实时推送更稳定

### 7. Grass

- **核心能力**：把 Claude Code 权限请求转发到手机 native modal
- **借鉴**：
  - ✅ 异步 mobile approval 是真实需求
  - ✅ 权限门（permission gate）比事后审计更安全

### 8. Ruflo / AssemblyZero / Neko-Gundan

- **共同特点**：企业/复杂场景的多 Agent swarm 编排
- **借鉴**：
  - ✅ 中央 orchestrator + worker agent 是标准模式
  - ✅ 需要审计、治理、成本控制能力

---

## 二、行业共识模式

| 模式 | 说明 | 我们采用的方案 |
|------|------|--------------|
| **Git Worktree 隔离** | 每个 Agent 独立工作区 | ✅ 每个 Agent 创建独立 worktree |
| **File-Based Handoff** | 通过文件传递上下文 | ✅ `.mac/` 目录作为标准协议 |
| **Pluggable Runtime** | 支持多种 coding agent | ✅ `AgentRuntime` 抽象接口 |
| **SQLite 协调** | 用数据库存状态、消息、审计 | ✅ SQLite 作为状态机 + 审计中枢 |
| **Mobile Approval** | 手机端审批关键操作 | ✅ 飞书卡片 + 审批链接 |
| **Quality Gates** | 阶段化自动检查 | ✅ 6 个 quality gates |
| **Audit Log** | 所有操作可审计 | ✅ executions/decisions/git_operations/tool_calls 表 |

---

## 三、为什么 CrewAI 不是核心

调研发现，Claude Code / ZCode / KimiCode 等 coding agent 已经自带：
- 文件读写
- 命令执行
- 测试运行
- 代码 review

因此主流做法是 **直接 orchestrate coding agent CLI**，而不是用 CrewAI 再包一层。CrewAI 更适合编排没有自带工具能力的纯 LLM Agent。

我们的方案：**AgentRuntime 可插拔**，CrewAI 可以作为未来一个 backend 选项，但不是 MVP 必需。

---

## 四、对我们设计的具体影响

1. **架构层**：从单一 Claude Code 调用改为 `AgentRuntime` 接口
2. **执行层**：每个 Agent 在独立 git worktree 中执行
3. **协调层**：SQLite 作为状态和审计中枢
4. **交互层**：飞书卡片实现异步审批
5. **质量层**：增加 Review Gate 和 Acceptance Gate
6. **安全层**：所有 Agent 操作写入审计日志

---

## 五、可直接使用的组件

| 组件 | 可用性 | 说明 |
|------|--------|------|
| Claude Code CLI | ✅ 本地已安装 | 作为主要 Agent Runtime |
| lark-oapi | ✅ Python SDK | 飞书 Bridge |
| GitPython | ✅ Python 库 | Git / worktree 操作 |
| Overstory 代码 | ⚠️ 可 fork | 已归档，改造成本高 |
| lark-channel-bridge | ⚠️ 可借鉴 | Node.js，只支持单 Agent |

**结论**：没有完全即插即用的方案，但各项目的设计模式可直接借鉴。自研是最高效的路径。
