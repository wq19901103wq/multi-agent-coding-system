# Worktree 管理规范

> 版本：v1.0  
> 用途：定义 Agent 独立工作区（git worktree）的命名、创建、同步、合并、清理规则。

## 命名规则

### 目录路径

```
projects/{project_id}/worktrees/{agent_type}_{stage?}_{task_id}_{timestamp}/
```

- `agent_type`：`planner` / `coder` / `tester` / `reviewer` / `acceptance_evaluator` / `project_manager`
- `stage`：planner 的 stage 名（`requirements` / `architecture` / `specs` / `bug_analysis` / `doc_outline`），其他 Agent 省略
- `task_id`：本次 `Task` 记录的唯一 ID，如 `task_20260707_001`
- `timestamp`：ISO 格式 `YYYYMMDD_HHMMSS`，使用 UTC

示例：
- `worktrees/planner_requirements_task_001_20260707_120000/`
- `worktrees/coder_task_002_20260707_120500/`
- `worktrees/tester_task_003_20260707_121000/`

### Git 分支名

```
mac/{project_id}/{agent_type}/{task_id}
```

示例：
- `mac/proj_20260707_001/planner/task_001`
- `mac/proj_20260707_001/coder/task_002`

分支基于主工作区当前分支（通常是 `main` 或 `master`）创建。

## 创建流程

1. Orchestrator 创建 `Task` 记录，分配 `task_id`。
2. 在主工作区执行：
   ```bash
   git worktree add \
     projects/{project_id}/worktrees/{worktree_name} \
     -b mac/{project_id}/{agent_type}/{task_id}
   ```
3. 将主工作区 `.mac/` 目录同步到 worktree：
   - 复制 `STATE.json`、`REQUIREMENTS.json`、`ARCHITECTURE.json`、`SPECS.json` 等
   - 复制 `TASK.json` 并更新 `task_id`、`agent_type`、`state`、`input`
4. 记录 `Task.worktree_path` 到数据库。
5. 写入 `event_logs`：`worktree_add` 操作。

## `.mac/` 同步规则

| 时机 | 动作 | 说明 |
|------|------|------|
| worktree 创建前 | Orchestrator 写 `STATE.json`、`TASK.json` | 基于当前项目状态 |
| worktree 创建后 | 复制 `.mac/*.json` 到 worktree | 只读副本，Agent 不可修改 |
| Agent 执行后 | Orchestrator 读取 worktree 的 `TASK.json` output | 主工作区 `.mac/TASK.json` 同步更新 |
| Agent 产物需要合并时 | 将产物文件从 worktree 复制/合并到主工作区 | 不合并 `.mac/` 中的 JSON（除 `TASK.json`） |

> **重要**：Agent 在 worktree 中 **不能** 修改 `STATE.json`。若 Agent 需要影响状态，必须通过 `TASK.json` 的 `output.next_state_event`。

## 合并规则

### 顺序合并

Reviewer 按 **Spec 顺序** 逐个合并 Coder/Tester worktree：

1. 取下一个待合并的 worktree
2. 在 **主工作区** 执行：
   ```bash
   git merge mac/{project_id}/{agent_type}/{task_id}
   ```
3. 若合并无冲突：
   - 保留 worktree 24 小时
   - 标记该 Spec 对应的 Task 为已合并
4. 若合并有冲突：
   - Reviewer Agent 尝试自动解决
   - 若自动解决失败，进入 `CONFLICT_PENDING`
   - 冲突 worktree 保留直到冲突解决

### 合并范围

- **代码/文档产物**：合并到主工作区 `src/`、`tests/`、`README.md` 等
- **`.mac/` 产物**：
  - `TASK.json`：合并后同步到主工作区 `.mac/TASK.json`
  - `REQUIREMENTS.json`、`ARCHITECTURE.json`、`SPECS.json` 等：由 planner 阶段直接写入主工作区，不通过 worktree 合并
- **测试产物**：Tester 生成的测试文件随代码一起合并

## 清理规则

1. **成功合并后**：worktree 保留 24 小时，之后由清理任务删除。
2. **失败后**：worktree 保留 7 天，供调试使用。
3. **清理命令**：
   ```bash
   git worktree remove {worktree_path}
   git branch -D mac/{project_id}/{agent_type}/{task_id}
   ```
4. **保留策略**：
   - `COMPLETED` / `FAILED` / `CANCELLED` 项目，7 天后清理所有 worktree
   - 手动 `/rollback` 时，当前 Coder worktree 保留 24 小时

## 并发控制

1. **同一项目**：同一时间只能有一个 Agent 在执行（顺序执行）。
2. **不同项目**：可以并发，但受全局运行时队列限制（见并发规范）。
3. **Reviewer 合并时**：禁止该项目进入新的 `CODING` 状态，避免分支混乱。

## 失败恢复

若 Agent 执行失败（JSON 解析错误、schema 校验失败、超时）：

1. 记录失败原因到 `Task.error_message`
2. 若 `retry_count < MAX_AGENT_RETRIES`：
   - 清理当前 worktree
   - 重新创建 worktree（新 task_id）
   - 在 `STATE.json` 中增加失败记录
3. 若超过最大重试：
   - 保留 worktree
   - 进入 `FAILED` 状态
   - 发送飞书通知
