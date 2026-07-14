# 会话绑定目录规范

> 版本：v2.2  
> 状态：核心绑定、继承、切换与隔离已实现；候选目录推荐卡待补；命令名 v2.2 重命名（`/bind-dir`→`/bind`、`/unbind-dir`→`/unbind`、`/current-dir` 合并进 `/current`）  
> 目标：让一个飞书聊天会话绑定一个本地 Git 仓库，之后在该会话创建的项目默认修改该仓库。

## 1. 核心语义

- 一个 `chat_id` 最多绑定一个目录，群成员共享这项绑定。
- 一个目录可以承载多个 MAC 项目；项目是一次交付任务，不等同于聊天窗口或代码仓库。
- 绑定只影响之后创建的项目。切换或解绑不会迁移、删除或改写已有项目。
- 未绑定时继续使用 `projects/{project_id}` 系统工作区。
- 用户可通过 `/switch <project_id>` 或 `/switch`（无参选择卡）在同一聊天中的多个项目间切换；目录绑定与当前项目选择是两种不同上下文。

## 2. 数据模型

目录是 chat 级属性，不能存入以 `(chat_id, open_id)` 为主键的 `ChatUserContext`，否则同一群的不同用户可能看到不同目录。使用独立表：

```text
ChatDirectoryBinding
  chat_id       PK
  directory     absolute path
  bound_by
  created_at
  updated_at
```

`Project.bound_directory` 保存项目创建时继承到的目录快照；`Project.workspace_path` 指向代码仓库根目录。解绑后项目仍使用该快照。

## 3. 多项目元数据隔离

多个项目不能共享同一个 `.mac/STATE.json`。绑定目录采用命名空间布局：

```text
repository/
  .git/
  .mac/
    projects/
      {project_a}/
        STATE.json
        REQUIREMENTS.json
        ARCHITECTURE.json
        SPECS.json
      {project_b}/
        STATE.json
        ...
    worktrees/
      {project_a}/...
      {project_b}/...
  src/
  tests/
```

系统工作区为保持兼容，仍使用原来的 `workspace/.mac/*.json` 和 `workspace/worktrees/`。

## 4. 当前交互

```text
/bind <绝对路径>  绑定或切换当前聊天的目录
/bind             推目录候选卡（扫描 MAC_ALLOWED_BIND_ROOTS + 手输兜底）
/current          查看当前聊天绑定目录与 active project
/unbind           解绑；只影响之后创建的项目
```
```

绑定成功后，当前聊天中的 `/create` 自动继承该目录。项目创建后仍自动成为该用户在当前聊天的 active project。

路径校验：

1. 必须是绝对路径。
2. 已存在时必须是目录。
3. 若位于另一个 Git 仓库内部，要求用户绑定真正的仓库根目录，避免嵌套仓库。
4. 非 Git 目录会初始化 Git，并为现有内容创建基线提交，使 Agent worktree 有有效 HEAD。
5. `.mac/worktrees/` 写入仓库本地 exclude，避免临时 worktree 被提交；项目产物不被整体忽略。

## 5. 目标候选目录卡

`/bind`（无参）触发候选目录卡，由 `project_manager` Agent + Bash + `bind_dir` 阶段实现，确定性扫描作为兜底：

- Handler 先调用 `project_manager` Agent（stage=`bind_dir`），prompt 注入 `MAC_ALLOWED_BIND_ROOTS` 严格约束扫描范围；
- Agent 用 Bash `ls`/`find` 在允许根目录下列出含 `.git` 的子目录，输出 `candidates` JSON；
- Handler 校验 Agent 返回的路径都在 `MAC_ALLOWED_BIND_ROOTS` 内、且确实含 `.git`，过滤越界路径；
- Agent 调用失败或返回空候选时，降级到确定性 Python 扫描（`ProjectService.scan_candidate_repositories`，递归 2 层，上限 12 个）；
- 卡片固定包含「手输绝对路径」和「取消」按钮；
- 用户点击候选 -> Handler 调用 `_prepare_bound_directory` 校验后写入 `ChatDirectoryBinding`；
- 用户点击「手输绝对路径」-> 设置 `PendingIntent.stage=awaiting_bind_dir`，下一条文本消息作为路径处理；
- 未配置 `MAC_ALLOWED_BIND_ROOTS` 时降级为提示用户使用 `/bind <绝对路径>`。

目录扫描严格限制在 `MAC_ALLOWED_BIND_ROOTS` 内。Agent 不直接写数据库、不执行 `git init`--所有写操作由 Handler 层确定性代码执行。

`run_chat_scoped_agent` 是 orchestrator 提供的 chat-scoped agent 调用通道：不创建 Task、不依赖 Project，使用临时目录作为 worktree，适用于 `/bind`（无参）这类没有项目上下文的控制面决策。

## 5A. `/create <描述>` 流程的 bind_dir 推荐

`/create <描述>`（含 `--type=` 显式指定）在创建项目前，如果当前会话尚未绑定目录且配置了 `MAC_ALLOWED_BIND_ROOTS`，会调用同一个 `project_manager` + `bind_dir` 阶段根据描述推荐匹配的代码仓库：

- 触发条件：会话无目录绑定、`MAC_ALLOWED_BIND_ROOTS` 非空、当前会话无活跃/暂停项目；
- Agent 接收 `description` 作为额外上下文，可在 `MAC_ALLOWED_BIND_ROOTS` 范围内推理匹配的仓库；
- Handler 层执行与 `/bind` 无参相同的路径校验（`_path_within_allowed_roots` + `.git` 存在性）；
- 返回有效候选时，发送「选择要绑定的目录」卡片（`build_bind_dir_for_create_card`），用户：
  - 点击候选 -> 绑定目录后创建项目；
  - 点击「跳过，使用系统工作区」-> 不绑定，直接用系统工作区创建项目；
- Agent 调用失败、返回空候选或所有候选被过滤时，不暂停，直接用系统工作区创建项目；
- 会话已绑定目录、未配置 `MAC_ALLOWED_BIND_ROOTS` 或存在活跃/暂停项目时，跳过 Agent 调用，直接创建项目。

`PendingIntent` 在暂停期间用 `stage=awaiting_bind_dir_for_create` 标识，`(task_type, description)` 以 JSON 编码存入 `description` 字段，避免 schema 迁移。

## 6. 并发与安全

- 同目录多个项目可以并存，但合并代码时仍受 Git 冲突约束。
- 每个项目的 Agent 分支名和 worktree 路径包含 `project_id`，不得复用。
- 群聊中只有绑定到该群的项目允许群成员操作；私聊仍要求 `ProjectUser` 绑定。
- API 直接传入 `bound_directory` 与飞书 `/bind` 走同一校验逻辑。
- 目录不存在时允许创建；绑定动作必须由用户明确触发。候选推荐本身不创建目录。
- 当前本地 MVP 假定飞书来源聊天和 Control API 都是可信控制面。进入多人生产环境前，必须增加"允许绑定的根目录"配置，并限制 `/bind` 为聊天管理员/系统 owner；否则任意群成员可能把 Agent 指向宿主机上的非预期目录。
- **目录切换/解绑受活跃项目约束**：当前 chat 存在 `active` 或 `paused` 状态项目时，`/bind <new-path>` 和 `/unbind` 必须拒绝，并提示阻塞项目列表。用户须先 `/cancel` 或等项目进入 `completed`/`failed`/`cancelled` 后才能切换目录。`completed`/`failed`/`cancelled` 项目不阻塞，因为其 `bound_directory` 快照已固化。

## 7. 验收标准

- 两个聊天可绑定不同目录，互不影响。
- 同一聊天后续两个项目继承同一仓库，但各自拥有独立 STATE 和规划产物。
- `/unbind` 后新项目回到系统工作区，旧项目仍指向原仓库。
- 绑定仓库中的 Agent worktree 能创建、合并和清理，不污染另一个项目的元数据。
- 服务重启后绑定关系和项目目录仍能恢复。
- chat 存在 `active`/`paused` 项目时，`/bind` 和 `/unbind` 必须拒绝并提示阻塞项目；`completed`/`failed`/`cancelled` 不阻塞。
