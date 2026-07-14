# Intent Router 规范

> 版本：v1.0  
> 用途：定义从用户自然语言描述推断任务类型（即选择状态机起点）的组件接口和行为。

## 职责

`IntentRouter` 只负责一件事：**根据用户输入的一句话描述，决定应该从哪个状态机起点开始执行**。它不等价于需求分析，只做一个粗分类：

- `feature` → `PLANNING_REQUIREMENTS`
- `bug_fix` → `BUG_ANALYSIS`
- `documentation` → `DOC_OUTLINE`

分类错误会导致进入错误的流水线，因此输出必须包含置信度和是否需要二次确认。

## 接口

```python
class RouteDecision(BaseModel):
    task_type: str | None          # 命中已知类型时填，否则 None
    initial_state: str | None      # task_type 对应的 initial_state
    confidence: float
    needs_disambiguation: bool     # 是否需要发确认卡
    reason: str | None


def route_demand(description: str) -> RouteDecision:
    """从描述推断任务类型。"""


def suggest_name(description: str) -> str:
    """从描述生成一个短项目名。"""
```

`needs_disambiguation` 由阈值决定：

```python
needs_disambiguation = low_threshold <= confidence < high_threshold
```

## 阈值配置

在 `config.py` 中配置：

```python
intent_high_threshold: float = Field(default=0.85, alias="INTENT_HIGH_THRESHOLD")
intent_low_threshold: float = Field(default=0.5, alias="INTENT_LOW_THRESHOLD")
```

三档处理：

| 档位 | 条件 | 行为 |
|------|------|------|
| 高置信 | `confidence >= intent_high_threshold` | 直接创建项目 |
| 中置信 | `intent_low_threshold <= confidence < intent_high_threshold` | 发送确认卡片，让用户确认/覆盖类型 |
| 低置信 / 未覆盖 | `confidence < intent_low_threshold` 或 `task_type is None` | 拒绝并追问用户重新描述 |

## 规则关键词（MVP 版本）

分类器优先按顺序匹配以下关键词。命中则停止匹配。

### bug_fix（置信度 0.85）

```
bug, fix, 修复, 报错, 异常, 崩溃, 不工作, 回归
```

### documentation（置信度 0.85）

```
文档, readme, agents.md, 说明文档, 写一份
```

### feature（置信度 0.85）

```
实现, 新增, 加一个, 开发, 支持
```

`feature` 同时是兜底类型：若描述未命中任何关键词，返回 `task_type=None`，置信度 0.4。

### 未覆盖类型

以下类型当前明确不支持，返回 `task_type=None`：

- `refactor`
- 纯调研 / 提问类描述
- 与代码无关的闲聊

## 匹配规则细节

- 英文关键词使用词边界匹配，避免 `bug` 命中 `debug`。
- 中文关键词使用子串匹配。
- 关键词按 `bug_fix → documentation → feature` 顺序匹配；一旦命中即停止。

## 可替换性

规则版本是 MVP 实现。后续可以替换为 LLM 分类器，只要保持 `route_demand(description) -> RouteDecision` 签名不变，调用方无需修改。

## 使用场景

### 飞书入口

用户通过飞书机器人创建项目时，只需描述需求，无需先选类型。`IntentRouter` 完成分类后：

- 高置信：直接调用 `ProjectService.create_project`
- 中置信：发送 `build_intent_confirm_card` 确认卡片
- 低置信：回复文本，引导用户重新描述或使用 `/create --type=<task_type> <描述>` 显式覆盖

### API 入口

`POST /projects` 保持 `task_type` 必填，不由 `IntentRouter` 处理。程序化调用方直接指定类型。

## 显式覆盖

飞书文本命令支持显式指定类型，跳过分类器：

```
/create --type=bug_fix 修复 port 校验范围
```

解析规则：

1. 去掉 `/create`
2. 若剩余文本以 `--type=` 开头，提取等号后的类型
3. 剩余部分作为 `description`
4. 直接以该类型创建项目

## 错误处理

- 若 `task_type` 对应的 `workflow_profiles/{task_type}.json` 不存在，视为未覆盖，返回 `task_type=None`。所有已覆盖类型均复用 `state_machines/delivery.json`。
- 分类器本身不应抛异常；任何异常都应被捕获并返回低置信度的 `RouteDecision`。

## 测试要求

1. 命中 bug/doc/feature 关键词时返回正确类型和 initial_state。
2. 未覆盖描述返回 `task_type=None`。
3. 英文词边界正确（如 `debug` 不命中 `bug`）。
4. `suggest_name` 能正确截断并清理首尾标点。
