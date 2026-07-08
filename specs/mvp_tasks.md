# MVP 任务规格

> 版本：v1.0  
> 用途：明确 3 个 MVP 真实任务的输入、预期产物和验收标准。

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
