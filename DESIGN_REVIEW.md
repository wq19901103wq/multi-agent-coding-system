# DESIGN.md 审查报告（spec-kit）

> **状态**：已修订，且架构有重大变更。本报告基于原始 CrewAI 方案，后续已按用户要求改为 **Claude Code 多实例架构**（每个 Specialist Agent 是一次 `claude -p` 调用）。报告中的问题清单仍有参考价值，但技术栈相关判断已不适用。

## 审查结论

- **总体判断**：有条件通过
- **最大风险**：CrewAI 与外部状态机的人工确认闭环集成细节严重不足，容易在 Phase 3-4 卡住

---

## 问题清单

| 维度 | 问题 | 影响 | 建议 |
|------|------|------|------|
| **架构** | 状态机定义了 `CLARIFICATION_PENDING` 和 `CONFLICT_PENDING`，但没有画出如何恢复执行（谁负责汇总回答、状态如何回到 CODING/SPEC_DECOMPOSITION） | 高 | 补充 pending 状态的退出转换和恢复机制 |
| **架构** | `Project Manager Agent` 职责范围过大，既做经理又生成确认卡片、又判定异常、又做根因分析 | 中 | 把卡片生成/状态推进放到 Python 控制层，Agent 只负责内容生成和推荐 |
| **接口** | 没有定义任何 Control API 端点、请求/响应 schema | 高 | 补充核心接口：`POST /projects`, `GET /projects/{id}`, `POST /projects/{id}/decisions`, `POST /webhook/feishu` |
| **接口** | 没有定义飞书卡片的 JSON 结构和回调事件解析规则 | 高 | 至少给出确认卡片和冲突卡片的字段结构 |
| **接口** | 没有定义 Agent 输出格式契约（requirements.md / architecture.md / specs/*.md 的具体字段） | 高 | 每个阶段定义输出模板，LLM 输出必须可被解析 |
| **安全** | Git 自动 merge 到 `main` 分支，没有 PR 或预审机制 | 高 | MVP 阶段至少提交到独立分支，或创建 PR 但不自动 merge |
| **安全** | Agent 在本地 Mac 执行测试和代码，没有沙箱 | 中 | 文档中明确风险，MVP 限定只执行 pytest，不执行任意 shell |
| **安全** | 没有 API 鉴权、飞书消息来源验证 | 中 | 补充 `FEISHU_VERIFICATION_TOKEN` 校验和基础访问控制 |
| **可行性** | Phase 4 「局部修改指令」（如「范围校验先不做」）没有解析机制 | 高 | 要么限定为预定义选项，要么需要一个「指令解析 Agent」 |
| **可行性** | 计划 Phase 2 做飞书卡片，但风险里又说「先做文本消息」 | 中 | 明确 MVP 是文本先行还是卡片先行，两者工作量差异大 |
| **可行性** | 10-14 天估算对单人开发偏乐观，特别是 CrewAI 调试 | 中 | 建议把 Phase 0-3 作为第一个里程碑，优先验证核心闭环 |
| **清晰性** | `MAC_HOST`, `MAC_PORT` 等环境变量命名容易和系统 `MAC` 地址混淆 | 低 | 改为 `MACS_HOST`, `MACS_PORT` 或 `MCS_*` |
| **清晰性** | `projects/` 目录同时被 `.gitignore` 忽略又用于 Git 目标仓库，逻辑矛盾 | 中 | 区分「系统代码仓库」和「Agent 生成项目的工作区」，后者不应提交到系统仓库 |

---

## 必须补充的 3 件事

1. **Control API 接口定义**：至少 4 个核心端点的请求/响应 schema
2. **LLM 输出契约**：requirements / architecture / specs / review / acceptance 的标准化输出格式
3. **Pending 状态恢复机制**：澄清和冲突如何退出、谁汇总、如何推进

---

## 建议优化

- 把 Git 自动 merge 改为「自动 commit + push 到 feature 分支」，合并到 main 作为后续增强
- Phase 0-3 优先跑通「无人工确认的自动流程」，Phase 4 再加飞书确认
- 为每个 Agent 输出定义 JSON/YAML 模板，用 Pydantic 校验
- 在架构图中把「状态机控制器」独立出来，不要让它隐含在 FastAPI 里
- 增加「测试用例执行策略」：Agent 生成测试后，是自动跑 pytest，还是先生成测试计划等人确认？
