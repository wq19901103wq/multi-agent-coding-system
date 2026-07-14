# 会话绑定目录规范

> 版本：v2.1  
> 状态：核心绑定、继承、切换与隔离已实现；候选目录推荐卡待补  
> 目标：让一个飞书聊天会话绑定一个本地 Git 仓库，之后在该会话创建的项目默认修改该仓库。

## 1. 核心语义

- 一个 `chat_id` 最多绑定一个目录，群成员共享这项绑定。
- 一个目录可以承载多个 MAC 项目；项目是一次交付任务，不等同于聊天窗口或代码仓库。
- 绑定只影响之后创建的项目。切换或解绑不会迁移、删除或改写已有项目。
- 未绑定时继续使用 `projects/{project_id}` 系统工作区。
- 用户可通过 `/use <project_id>` 在同一聊天中的多个项目间切换；目录绑定与当前项目选择是两种不同上下文。

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
/bind-dir <绝对路径>  绑定或切换当前聊天的目录
/current-dir          查看当前聊天绑定
/unbind-dir           解绑；只影响之后创建的项目
```

绑定成功后，当前聊天中的 `/create` 自动继承该目录。项目创建后仍自动成为该用户在当前聊天的 active project。

路径校验：

1. 必须是绝对路径。
2. 已存在时必须是目录。
3. 若位于另一个 Git 仓库内部，要求用户绑定真正的仓库根目录，避免嵌套仓库。
4. 非 Git 目录会初始化 Git，并为现有内容创建基线提交，使 Agent worktree 有有效 HEAD。
5. `.mac/worktrees/` 写入仓库本地 exclude，避免临时 worktree 被提交；项目产物不被整体忽略。

## 5. 目标候选目录卡

首次 `/create` 且聊天未绑定目录时，后续版本可展示一次性选择卡：

- 检测到的已有仓库；
- 根据需求推断的新目录；
- 用户手工输入绝对路径；
- 不绑定，使用系统工作区。

目录扫描必须限制在配置允许的根目录内，不能让业务 Agent 任意遍历整台机器。候选选择复用 `QuestionInteraction`，但最终绑定由确定性路径校验代码执行，不由 Agent 直接写数据库或执行 `git init`。

## 6. 并发与安全

- 同目录多个项目可以并存，但合并代码时仍受 Git 冲突约束。
- 每个项目的 Agent 分支名和 worktree 路径包含 `project_id`，不得复用。
- 群聊中只有绑定到该群的项目允许群成员操作；私聊仍要求 `ProjectUser` 绑定。
- API 直接传入 `bound_directory` 与飞书 `/bind-dir` 走同一校验逻辑。
- 目录不存在时允许创建；绑定动作必须由用户明确触发。候选推荐本身不创建目录。
- 当前本地 MVP 假定飞书来源聊天和 Control API 都是可信控制面。进入多人生产环境前，必须增加“允许绑定的根目录”配置，并限制 `/bind-dir` 为聊天管理员/系统 owner；否则任意群成员可能把 Agent 指向宿主机上的非预期目录。

## 7. 验收标准

- 两个聊天可绑定不同目录，互不影响。
- 同一聊天后续两个项目继承同一仓库，但各自拥有独立 STATE 和规划产物。
- `/unbind-dir` 后新项目回到系统工作区，旧项目仍指向原仓库。
- 绑定仓库中的 Agent worktree 能创建、合并和清理，不污染另一个项目的元数据。
- 服务重启后绑定关系和项目目录仍能恢复。
