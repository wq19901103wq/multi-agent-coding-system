# MVP 任务规格

> 版本：v2.0
> 用途：明确 3 个 MVP 真实任务的输入、预期产物和验收标准。

---

## 状态机

新状态机包含 8 个确认节点，状态流转如下：

INIT → PLANNING_REQUIREMENTS → REQUIREMENT_INPUT_PENDING → REQUIREMENT_CONFIRM_PENDING → PLANNING_ARCHITECTURE → ARCHITECTURE_INPUT_PENDING → ARCHITECTURE_CONFIRM_PENDING → PLANNING_SPECS → SPEC_INPUT_PENDING → SPEC_CONFIRM_PENDING → CODING → CODING_CONFIRM_PENDING → TESTING → TESTING_CONFIRM_PENDING → REVIEW → REVIEW_CONFIRM_PENDING → REWORK → ACCEPTANCE_PENDING → MERGING → COMPLETED

其中质量门（CODING_CONFIRM_PENDING、TESTING_CONFIRM_PENDING、REVIEW_CONFIRM_PENDING）需要人工确认，驳回走 REWORK 路径。

---

## Task 1：config-validator CLI（feature）

### 输入

```json
{
  "task_type": "feature",
  "description": "实现一个 config-validator CLI 工具，读取 config.json 和 schema.json，校验必填字段、类型、数值范围，输出 text 或 json 格式。"
}
```

### 预期产物

```
projects/{project_id}/src/config_validator/
├── __init__.py
├── __main__.py
├── cli.py
├── schema.py
├── validator.py
└── formatter.py
projects/{project_id}/tests/
├── test_validator.py
├── test_schema.py
└── test_cli.py
projects/{project_id}/README.md
projects/{project_id}/pyproject.toml
```

### 功能要求

- 命令：`python -m config_validator config.json schema.json [--format json]`
- 校验规则：
  - `required`：必填字段
  - `type`：`string`, `integer`, `boolean`, `list`
  - `minimum` / `maximum`：仅对 `integer` 生效
- 输出格式：默认 text，`--format json` 输出 JSON
- 退出码：通过返回 0，失败返回 1

### 验收标准

- `pytest` 全部通过
- 覆盖正常、缺少必填字段、类型错误、范围越界场景
- 手动运行命令验证通过
- 代码提交到 feature 分支

---

## Task 2：Port 范围 bug fix（bug_fix）

### 输入

```json
{
  "task_type": "bug_fix",
  "description": "修复 config-validator 的 port 范围校验 bug：当前实现可能未包含 0 或 65535，应改为 0-65535。"
}
```

### 前置状态

假设 Task 1 的代码已存在于项目主工作区。

### 输入样本

`schema.json`（复用 Task 1）：

```json
{
  "required": ["name", "port"],
  "properties": {
    "name": {"type": "string"},
    "port": {"type": "integer", "minimum": 0, "maximum": 65535}
  }
}
```

`config_port_0.json`：

```json
{"name": "my-app", "port": 0}
```

`config_port_65535.json`：

```json
{"name": "my-app", "port": 65535}
```

### 预期修改

- `src/config_validator/validator.py`：将 port 范围判断改为 `0 <= port <= 65535`
- `tests/test_validator.py`：新增边界测试用例

### 新增测试用例

| 输入 | 预期结果 |
|------|---------|
| `port: 0` | 通过 |
| `port: 65535` | 通过 |
| `port: -1` | 失败，提示超出范围 |
| `port: 65536` | 失败，提示超出范围 |

### 验收标准

- 新增边界测试通过
- 原有测试全部通过
- 手动验证 `python -m config_validator config_with_port_0.json schema.json` 通过
- 代码提交到 feature 分支

---

## Task 3：AGENTS.md（documentation）

### 输入

```json
{
  "task_type": "documentation",
  "description": "为项目写一份 AGENTS.md，作为 AI 助手的行为守则。参考项目根目录的 AGENTS.md 风格，但针对本项目（multi-agent-coding-system）。"
}
```

### 预期产物

```
projects/{project_id}/AGENTS.md
```

### 内容要求

- 核心纪律（严禁擅自执行、严禁超出范围、严禁隐瞒结果等）
- 文件操作规范（只修改 `.mac/` 和 `src/` 内文件，沙箱外只读）
- Agent 协作规范（通过 `.mac/` 文件协议交换产物，不直接修改 `STATE.json`）
- Git 规范（提交到 feature 分支，不自动 merge 到 main）
- 飞书确认响应规范（确认/驳回/追问）

### 验收标准

- 文档结构完整，包含上述章节
- 语言风格与项目根目录 `AGENTS.md` 一致
- 提交到 feature 分支

---

## MAC 系统级验收场景

前三个任务验证 Agent 能生成产物；以下场景验证 MAC 系统自身。未通过这些场景时，不能宣称 MVP 状态机、飞书闭环或多 Agent 反馈链路完成。

### S1：需求到验收追踪

- 创建至少包含 3 条 REQ、5 条 AC 的 feature 项目。
- Architecture DEC、Specs、Tester evidence、Reviewer coverage 和 Acceptance Matrix 均能引用稳定 ID。
- 故意移除一个 AC 的测试，Acceptance Evaluator 必须阻止 confirm 并指出缺失证据。

### S2：需求多轮与失效传播

- Requirements Agent 主动提问，用户分两轮回答后确认需求 v2。
- Architecture/Specs 生成后，用户修改一条已确认 AC。
- 系统必须标记受影响 DEC/SPEC/Task/Test 为 stale，使旧 approval 过期，并生成 impact report。
- 未受影响 Spec 可保留，但必须有重新验证记录。

### S3：多聊天防串话

- 同一用户在群 A 和私聊 B 分别选择不同项目并发送自由文本。
- 输入必须进入各自 active project，不得按最近更新时间串入另一项目。
- 同一群聊两名用户分别选择不同项目，彼此上下文不得互相覆盖。

### S4：多聊天同一审批

- 同一 approval 投递到来源群和 reviewer 私聊。
- 两处并发响应时只有一个状态转换成功；另一方获得幂等“已处理”结果。
- 其他聊天中的旧卡片更新为已处理，更新失败不回滚项目状态。

### S5：运行中用户输入

- CODING 运行期间用户追加 scope change。
- 输入持久化并固化原 project_id；用户随后 `/use` 切换项目不改变该输入归属。
- Coder 返回后系统在 merge/transition 前消费输入，使旧结果失效并进入 REWORK/上游对齐。

### S6：测试失败归因

- 分别模拟 implementation_bug、test_bug、environment_failure、spec_ambiguity。
- 系统推荐 Coding、Testing、Testing/人工、Specs 等不同回退目标。
- 明确 fail 在 night/auto 模式下也不得自动确认。

### S7：Review 反馈闭环

- Reviewer 产生两个 blocking issue 和一个 non-blocking issue。
- Coder 下一轮必须收到完整 feedback IDs，并逐项输出 fixed/disputed。
- Reviewer 二次验证旧 issue；存在未解决 blocking issue 时不得 pass。

### S8：恢复与幂等

- 分别在 running Task、pending approval、业务事务提交后消息发送前重启服务。
- 数据库恢复后不得重复创建项目、Task、Approval 或状态转换。
- `STATE.json` 可由数据库重建；重复飞书 event_id 被安全忽略。

### S9：权限与敏感通知

- viewer 不能 rollback/cancel/confirm；reviewer 只能处理授权质量门；owner 可执行最终验收。
- 群聊不展示成本、内部绝对路径和敏感错误；这些内容只发 owner 私聊。

### S10：状态机结构校验

- 所有 next_state 存在，所有非终态可达终态，不存在不可达状态或无出口非等待状态。
- Spec dependencies 无环。
- quality fail 不指向 pass confirmation；REWORK/最终验收不能自动确认。

### S11：全 Agent Grill-Me

实现范围：统一 question/answer 数据模型、共享 JSON Schema、通用 `AGENT_INPUT_PENDING` 覆盖层、API/飞书原子回答入口，以及所有 agent-execution 状态的恢复逻辑；只修改 prompt 而未完成上述链路不算交付。

- Requirements Agent 连续提出两轮范围/非功能问题，第二轮后继续执行。
- Architecture Agent 提供三个选型方案、推荐项和影响，用户选择非推荐项后方案按选择更新。
- Specs Agent 就兼容策略提问并恢复原阶段。
- Coder 就破坏性迁移提问；回答只进入下一轮 Coder。
- Tester 就真实环境或 mock 选择提问；用户自由文本补充环境约束。
- Reviewer 就 blocking issue 选择修复、风险接受或回退；风险接受必须校验 owner 权限。
- Acceptance Evaluator 请求人工体验结论或补充证据。
- Project Manager 在多个聊天/项目/approval 候选时要求用户选择，禁止猜测。
- 每个场景验证选项、自由文本、accept_default、服务重启恢复和三轮连续问答无重复 approval。
