# Multi-Agent Coding System (MAC System)

一个条件人类闭环的多 Agent 编码系统，覆盖从需求分析到自动合并提交的完整软件交付流程。

## 核心特性

- **完整流程**：需求分析 → 技术规划 → Spec 拆分 → 代码实现 → 测试 → Review → 验收 → 自动 Git 合并
- **条件人类闭环**：仅在 4 个关键节点请求人工确认（需求确认、方案确认、澄清/冲突、验收确认）
- **移动端审核**：通过飞书机器人接收确认卡片、追问、查看状态
- **根因定位**：验收失败时可追溯是需求、Spec、实现还是测试的问题

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
│   ├── bridge/           # 飞书 Bridge
│   ├── core/             # 状态机、数据库、Git 操作
│   ├── agents/           # CrewAI Agent 定义
│   ├── models/           # 数据模型
│   └── tasks/            # CrewAI Task 定义
├── tests/                # 测试
├── projects/             # Agent 生成的项目工作区（每个子目录是独立 Git 仓库）
└── DESIGN.md             # 架构设计文档
```
