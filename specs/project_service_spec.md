# ProjectService 接口规范

> 版本：v1.0  
> 用途：定义项目生命周期管理的服务层接口，供 Control API 和 Orchestrator 调用。

## 职责

`ProjectService` 是业务逻辑层，负责：

1. 创建项目并初始化工作区
2. 查询项目状态和产物
3. 记录人工决策
4. 更新状态机状态
5. 管理 `Task` 和 `Approval` 记录

## 接口

```python
class ProjectService:
    async def create_project(
        self,
        name: str | None,
        task_type: str,
        description: str,
        approval_mode: str = "normal",
        creator_open_id: str | None = None,
    ) -> Project:
        """创建项目，初始化目录、git、STATE.json。"""

    async def get_project(self, project_id: str) -> Project | None:
        """查询项目。"""

    async def get_project_status(self, project_id: str) -> dict:
        """返回项目当前状态、最近事件、pending 审批。"""

    async def transition_state(
        self,
        project_id: str,
        event: str,
        payload: dict | None = None,
        triggered_by: str = "system",
    ) -> Project:
        """根据状态机和事件推进/回退状态。"""

    async def create_task(
        self,
        project_id: str,
        agent_type: str,
        state: str,
        input_data: dict,
    ) -> Task:
        """创建 Agent 执行 Task 记录。"""

    async def complete_task(
        self,
        task_id: str,
        output_data: dict,
        cost_info: dict | None = None,
    ) -> Task:
        """标记 Task 完成。"""

    async def fail_task(
        self,
        task_id: str,
        error_message: str,
    ) -> Task:
        """标记 Task 失败，增加 retry_count。"""

    async def create_approval(
        self,
        project_id: str,
        task_id: str | None,
        state: str,
        approval_type: str,
        question: str,
        options: list[dict],
        payload: dict | None = None,
    ) -> Approval:
        """创建人工审批记录。"""

    async def respond_approval(
        self,
        approval_id: str,
        response: str,
        responded_by: str,
    ) -> Approval:
        """记录用户对审批的响应。"""

    async def resolve_approval(
        self,
        approval_id: str,
        action: str,
        responded_by: str,
        expected_state: str,
        payload: dict | None = None,
    ) -> Project:
        """原子校验并响应审批、失效旧审批、转换项目状态。"""

    async def log_event(
        self,
        project_id: str,
        level: str,
        message: str,
        meta: dict | None = None,
    ) -> None:
        """写入审计日志。"""
```

## 项目创建流程

1. 生成 `project_id`：`proj_{timestamp}_{random_suffix}`
2. 创建目录：`projects/{project_id}/`
3. 创建子目录：`.mac/`、`src/`、`tests/`、`worktrees/`
4. 执行 `git init`
5. 配置 git user：`git config user.name`、`git config user.email`
6. 写入初始 `STATE.json`：
   - `current_state = "INIT"`
   - `context = {task_type, description, approval_mode, retry_counts: {}}`
7. 创建 `Project` 数据库记录
8. 若提供了 `creator_open_id`，创建 `project_users` 绑定
9. 自动触发 `INIT.start` → 任务类型对应的初始状态：
   - `feature` → `PLANNING_REQUIREMENTS`
   - `bug_fix` → `BUG_ANALYSIS`
   - `documentation` → `DOC_OUTLINE`

## 状态转换流程

1. 加载当前项目状态和状态机配置
2. 调用 `StateMachine.next_state(current_state, event)`
3. 若返回新状态：
   - 更新 `Project.current_state` 和 `Project.previous_state`
   - 更新 `STATE.json`
   - 记录 `event_logs`
   - 根据新状态类型触发后续动作（Agent 执行 / 发送卡片）
4. 若事件非法：
   - 抛出 `InvalidStateTransitionError`
   - 状态不变

## 一致性与幂等约束

1. 数据库中的 `Project`、`Task`、`Approval` 和用户输入记录是控制层真相源；`STATE.json` 是可重建快照。
2. 同一项目最多一个 `status=pending` 的 Approval。创建前若同状态已有 pending 记录则复用；不同状态的旧记录标记为 `expired`。
3. `resolve_approval` 在一个事务内校验 approval 状态、项目当前状态和允许 action，然后同时提交 approval 响应与状态转换。
4. 重复请求不得二次转换；服务返回第一次处理后的项目状态和 `already_resolved=true`。
5. 每次状态转换都带 `expected_current_state` 或状态版本号，防止旧 Agent、旧卡片覆盖新状态。
6. `STATE.json` 在事务成功后以临时文件加原子 rename 刷新；写入失败记录恢复事件，服务启动时从数据库重建。

## PendingUserInput

ProjectService 提供创建、查询和消费中途输入的接口。输入只追加、不覆盖，至少保存发送者、原始内容、观察到的状态、分类、处理状态、消费 Task 和时间戳。取消项目后到达的输入保留审计记录但标记为 `ignored_cancelled`。

## 会话路由接口

ProjectService 或独立 ConversationService 提供：

```python
bind_project_user(project_id, open_id, role)
bind_project_chat(project_id, chat_id, chat_type, relation, notification_level)
set_active_project(chat_id, open_id, project_id | None)
resolve_message_project(chat_id, open_id, explicit_project_id=None) -> Project | Ambiguous
list_projects_for_context(chat_id, open_id) -> list[Project]
```

权限关系、聊天订阅和 active context 分开持久化。`resolve_message_project` 不得回退到“用户最近更新项目”；无唯一上下文时返回 Ambiguous，由交互层展示选择卡片。

## ID 生成规则

| 实体 | 格式 | 示例 |
|------|------|------|
| project_id | `proj_{YYYYMMDD}_{HHMMSS}_{random_4}` | `proj_20260707_120000_a3f7` |
| task_id | `task_{YYYYMMDD}_{HHMMSS}_{random_4}` | `task_20260707_120100_b2c9` |
| approval_id | `appr_{YYYYMMDD}_{HHMMSS}_{random_4}` | `appr_20260707_120200_d1e5` |

`random_4` 为小写十六进制字符串。
