# AGENTS.md — MAC System 开发纪律

## 项目当前状态（2026-07-19）

### 仓库

- **GitHub**：https://github.com/wq19901103wq/multi-agent-coding-system（public）
- **默认分支**：`main`（历史已 scrub，无密钥）
- **开发分支**：`feat/core`（与 main 同步）
- **本地备份分支**（含旧 secret，**禁止 push**）：`main-backup-before-overwrite`、`main-pre-scrub-backup`、`feat/core-backup-before-rebase`

### 已完成

- **Session Binding v2.2（已合并到 main）**：
  - 命令重命名：`/bind-dir`->`/bind`、`/unbind-dir`->`/unbind`、`/current-dir` 合并进 `/current`
  - 活跃/暂停项目约束：`/bind` 和 `/unbind` 在 chat 有 active/paused 项目时拒绝
  - `/switch`（无参）项目选择卡、`/bind`（无参）候选目录卡（`project_manager` Agent + `bind_dir` 阶段 + 确定性兜底）
  - `/create <描述>` 流程集成 bind_dir agent 推荐：会话无绑定 + `MAC_ALLOWED_BIND_ROOTS` 非空时，Agent 根据描述推理匹配仓库
  - 安全模型：Agent 只读扫描，Handler 层 `_path_within_allowed_roots` + `.git` 校验过滤，Agent 不写 DB、不执行 git init
- **状态机重构（v2.0）**：`feature` / `bug_fix` / `documentation` 共用 `state_machines/delivery.json`，差异放在 `workflow_profiles/`；编译后的流程继续支持：
  - 规划阶段输入态：`REQUIREMENT_INPUT_PENDING` / `ARCHITECTURE_INPUT_PENDING` / `IMPLEMENTATION_SPEC_INPUT_PENDING`（及 bug_fix / documentation 对应输入态）
  - 质量确认关：`CODING_CONFIRM_PENDING` / `TESTING_CONFIRM_PENDING` / `REVIEW_CONFIRM_PENDING`
  - 统一的 `REWORK` 状态，支持回退到任意上游阶段（含 `to_testing`）
  - 质量失败统一走 `fail` -> `REWORK`，只有明确 `pass` 才进入确认关
- **汇报卡片与确认卡片分离**：Agent 执行进入 `human_confirmation` 后，必须先发送汇报卡片（`send_content_card`）展示本轮输出，再发送带操作按钮的确认卡片（`send_confirmation_card`）。
- **Night / Auto 模式质量关自动通过**：同一 tick 内自动 confirm，不阻塞流程；但仅当 Agent 明确判定 pass 时才生效。
- **REWORK 状态落地**：人工选择回退目标（需求 / 架构 / Spec / 编码 / 测试）。
- **结构化反馈模型**：新增 `FeedbackPacket` 与 `PendingUserInput`，替代脆弱的 `latest_rejection_feedback` 字符串。
- **Orchestrator 核心改造**：
  - Tester / Reviewer 判定失败时创建 `FeedbackPacket` 并进入 `REWORK`
  - 质量关通过时按 `verified_feedback_ids` 关闭历史反馈
  - Agent 运行期间收到的自由文本/命令被消费为 `PendingUserInput`，scope_change/correction 会强制质量关进入 `REWORK`
  - 每轮任务记录 `round_number`，旧 approval 在状态重新进入时标记 `expired`
  - 用户在问题卡中授权运行时能力时，选项必须携带结构化 `grants_runtime_capabilities`；Handler 将授权按项目/Agent/状态持久化并由 Runtime 执行，严禁从选项关键词或文案猜权限
- **Profile 系统校验**：新增 `StateMachine.validate()` 与回归测试，确保所有 `workflow_profiles/*.json` 可解析、阶段 ID 唯一、rework/acceptance 目标存在、编译后状态机无悬空跳转。
- **单元测试基线**：**283 passed**（2026-07-19 复跑确认）。
- **真实飞书端到端**：`full_feature_flow` 已在最新代码上全分支跑通到 `COMPLETED`（2026-07-19，`proj_20260719_020155_d373`：规划选择题、需求/Spec 驳回、编码/测试/评审 REWORK 回退、验收回需求全量重放）；`requirement_custom_question` / `planning_rejection_form` / `architecture_confirmation` / `acceptance_question_card` / `runtime_recovery_card` / `conversation_delivery` 场景均已真实飞书验证；脚本已能自动接管质量 Agent 主动 fail 的 REWORK。
  - 注意：**同一聊天窗口不要并发跑多个 e2e scenario**，否则 simulator 会把不同项目的 approval 卡片串扰。

### 已踩坑 & 必须规避

1. **汇报卡片被吞事件**
   - 根因：`_send_confirmation_cards` 把 `page` / `total` 传成 `None`，`build_content_card` 里 `total <= 1` 比较时报 `TypeError`，异常被吞后只剩确认卡片。
   - 结论：卡片参数必须传有效值；任何“显示优化”应在 builder 内部处理，不要传 `None`。

2. **uvicorn `--reload` 导致灾难**
   - Agent 会在 `projects/` 下大量写文件，`--reload` 监听到后会反复重启服务，导致 SQLite `database locked`、tick 中断、飞书消息处理异常。
   - 结论：验证/生产启动**禁止**用 `--reload`。如本地开发确实需要热重载，必须限制 `--reload-dir src/`。

3. **Editable install 缓存**
   - 只改源码不 `pip install -e .` 并重启，运行的是旧 `.pyc`/缓存代码。
   - 结论：每次代码修改后必须 reinstall 并重启 uvicorn。

4. **只看 e2e 脚本 `COMPLETED` 不够**
   - 之前 full flow 到了 `COMPLETED`，但汇报卡片实际被吞。
   - 结论：改完卡片/飞书交互后，必须检查最近的 `server_*.log` 中是否存在 `Failed to send summary content card` 或 `NoneType` 警告，并在飞书侧确认两种卡片都收到。

5. **Prompt 里不要写相对 schema 路径**
   - `planner_*.md` 原提示把 schema 路径写成 `{{workspace_path}}/specs/schemas/xxx.json`，但项目 workspace 下并没有这个目录，导致 Agent 找 schema 耗时甚至失败/重试。
   - 结论：prompt 中 schema 路径统一用渲染器注入的 `{{output_schema_path}}`，它指向仓库根目录的绝对路径。

6. **FEISHU_TEST_MODE 不要写进 .env**
   - `.env` 里留 `FEISHU_TEST_MODE=1` 会导致 `test_inject_card_action_disabled_by_default` 和 `test_night_mode_outside_window_falls_back_to_normal` 失败。
   - 根因：`Settings` 从 `.env` 加载，`get_settings()` 用 `@lru_cache`，单测里也拿到 `feishu_test_mode=True`，导致 test 端点返回 200（而非 404）+ night mode 在窗口外不回退 normal。
   - 结论：`.env` 不写 `FEISHU_TEST_MODE`；E2E 时用 `export FEISHU_TEST_MODE=1` 临时设置。

7. **推远端前必须扫密钥 + 查历史**
   - `.env.bak.20260711_182329` 曾被 git 跟踪，含真实 Feishu App Secret，在 commit `42a91cb` 就进去了。
   - `.gitignore` 只忽略 `.env`/`.env.local`，没忽略 `.env.bak.*`；备份文件漏网。
   - 结论：推远端前跑 `git ls-files | xargs grep -lE "<secret-pattern>"` 扫当前 tree，再跑 `git log -p --all | grep -c "<secret>"` 扫历史。`.gitignore` 要覆盖 `.env.bak`、`.env.bak.*`。历史里有密钥用 `git filter-repo --path <file> --invert-paths --refs <branch>` 抹掉。

8. **SQLite WAL sidecar 文件必须 gitignore**
   - `mac_system.db-shm` 和 `mac_system.db-wal` 曾被意外提交，运行时不断变化。
   - `.gitignore` 有 `*.db` 但没有 `*.db-shm`/`*.db-wal`。
   - 结论：`.gitignore` 必须加 `*.db-shm` 和 `*.db-wal`。

9. **长时间分叉的分支不要硬 rebase**
   - feat/core 和 main 对 ask_user 功能有完全不兼容的实现（main 无 `QuestionInteraction` 模型），rebase 时 13+ 文件冲突且不是格式差异。
   - 结论：两分支对同一功能有不兼容实现时，rebase 几乎注定失败。尽早和用户对齐"保留哪边"，必要时 force-overwrite（先备份 `git branch <name>-backup`）。

10. **私聊入口锁不能覆盖 Agent 长任务**
   - 自然语言确认、驳回或返工也可能在同一个消息处理协程里启动耗时 Agent；只放开卡片事件仍会让后续私聊排队数分钟。
   - 结论：入口串行化只能是非阻塞、机会式的；锁已被占用时，后续状态询问、纠正、取消和无关问题必须继续进入。审批与交互去重依赖数据库中的原子 claim 和幂等键，不能依赖覆盖整个 Agent 生命周期的进程锁。
   - 回归要求：卡片触发长任务和自然语言触发长任务两种路径，都必须验证后续文本能在长任务结束前被处理。

---

## 每次修改后的验证流程

### 重要定义

> 测试术语以 `docs/testing-taxonomy.md` 为唯一规范。禁止单独使用“测试通过”“真实飞书 E2E”或“黑盒通过”。报告必须明确 T1-T9 层级、会话类型、操作者、是否经过真实飞书、是否使用测试注入端点和仍未证明的内容。
>
> 特别注意：`scripts/feishu_e2e.py` 当前主要属于 **T4 真实飞书传输 E2E**（真实飞书群 + Simulator Bot + 真实 LLM，可使用测试 action 注入），**不是 T5 真实私聊界面自动黑盒**。用户要求“在私聊中像人一样点击、输入、发送”时，必须执行 T5，不能用 T4 替代或宣称私聊通过。

验证流程按改动适用层级执行；涉及私聊的改动，T4 与 T5 缺一不可：

1. **单元测试全绿**

   ```bash
   cd /Users/yihanwang/multi-agent-coding-system
   source .worktrees/feat-core/.venv/bin/activate
   pytest tests/ -q
   ```

   当前基线：**513 passed**（2026-07-28）。

2. **T4 真实飞书传输端到端验证（涉及飞书/流程时必须）**

   ```bash
   source .worktrees/feat-core/.venv/bin/activate
   set -a && source .env && set +a
   python scripts/feishu_e2e.py --scenario full_feature_flow
   ```

   必须等待 scenario 输出 `reached COMPLETED`，并在真实飞书群确认完整交互过程。但该结论只能记为 T4，不能自动升级为 T5 私聊 UI 黑盒。

   **T4 分支覆盖要求**：
   - `full_feature_flow` 场景必须覆盖每个确认节点的 **confirm** 路径
   - 后续需逐步加入 **reject**（驳回回退）、**select_option**（方案选项选择）、**rework_to 各阶段**（回退到需求/架构/Spec/编码/测试）分支
   - 若 `full_feature_flow` 尚未覆盖某个分支，应**单独编写对应 scenario** 并纳入 `--all` 执行

   **T4 外部依赖处理**：
   - 若因 API 额度、网络、模型端点等外部原因失败，需如实记录失败原因并告知用户，不得隐瞒或宣称”已完成”。
   - 若修改的是状态机、卡片交互或飞书命令处理，`full_feature_flow` 必须能覆盖新增的质量确认关卡（`CODING_CONFIRM_PENDING` / `TESTING_CONFIRM_PENDING` / `REVIEW_CONFIRM_PENDING`）。
   - 修改 `scripts/feishu_e2e.py` 本身时，应确保 `full_feature_flow` 随新流程同步更新。
   - **额外检查**：跑完后搜索最近的 `server_*.log`，确认没有 `Failed to send summary content card` 或 `NoneType.*int` 警告。

3. **T5 真实私聊界面自动黑盒（涉及私聊/Coordinator 或用户明确要求时必须）**

   必须从用户可见的 Bot 私聊窗口，通过桌面 UI 点击、输入、发送和点击卡片。禁止用 Simulator Bot、MAC API、测试注入端点或数据库写入代替交互。失败后可进入 T9 诊断，但修复后必须重新从私聊 UI 验证。

4. **服务重启确认**

   若修改影响运行中的 API / Feishu WebSocket 服务，验证前必须重启服务并确认：

   - 端口 `18080` 正常监听；
   - `/admin/projects` 返回 200；
   - Feishu WebSocket 已连接；
   - 未使用 `--reload` 启动。

---

## 关键配置

| 项 | 值 |
|---|---|
| Python | 3.10 |
| 虚拟环境 | `.worktrees/feat-core/.venv/`（editable install 指向仓库根 `src/`） |
| 服务端口 | `18080`（`.env` 中 `MCS_PORT=18080`） |
| 框架 | FastAPI + Uvicorn |
| 数据库 | SQLite `mac_system.db` |
| 项目目录 | `projects/` |
| 飞书接入 | WebSocket 长连接（`FEISHU_TRANSPORT=websocket`） |
| 飞书凭证 | `.env` 中真实 App ID / Secret |
| Admin 面板 | `http://127.0.0.1:18080/admin/projects` |
| LLM 运行时 | Claude Code via stream-json；Kimi endpoint via local proxy |

---

## 常用命令

### 1. 启动服务（验证/生产用，**不带 reload**）

```bash
cd /Users/yihanwang/multi-agent-coding-system
./scripts/start_server.sh
```

启动脚本会生成带时间戳的日志文件，例如 `server_20260718_152800.log`，并通过 `logging.yaml` 自动追加并按 10 MB 轮转（保留 5 个备份），重启不会覆盖上一会话的日志。

启动后检查：

```bash
cat uvicorn.pid
lsof -Pi :18080 -sTCP:LISTEN
ls -lt server_*.log | head -5
tail -20 server_*.log
```

### 2. 停止服务

```bash
./scripts/stop_server.sh
```

### 3. 单元测试

```bash
pytest tests/test_feishu.py tests/test_api.py tests/test_orchestrator.py -q
```

### 4. 真实飞书 full flow

```bash
set -a && source .env && set +a
python scripts/feishu_e2e.py --scenario full_feature_flow
```

### 5. 检查汇报卡片是否发送成功

```bash
grep -E "Failed to send summary content card|NoneType.*int" server_*.log
```

无输出为正常。

---

## 容错问题状态（2026-07-15）

### 1. 服务重启中断运行中任务（已修复）
启动时会标记 stale running task、重新调度对应项目，并修复终态 status 与缺失的人机审批。

### 2. `fail_max_retries` 状态机事件缺失（已修复）
统一 `delivery.json` 的 quality policy 会编译出 `fail_max_retries -> FAILED`。

### 3. linter 文件写入冲突
Agent 写入 `.mac/` 文件后 linter 自动修改，导致下次写前校验失败。需在 worktree 层面跳过 linter。

### 4. 自由文本被卡住项目劫持（已修复）
只有当前状态确有 running Agent task 时才创建 pending input；确认态文本会路由到输入审批或提示显式确认/驳回。

详细分析见 memory: `known-blockers.md`。

## 纪律红线

- 未经用户逐条确认，不得修改源码、prompt、配置、环境变量，不得启动/停止服务，不得删除/覆盖数据。
- 只做与当前任务相关的最小修改，禁止“顺手重构”。
- 改完后必须自己验证，不得把验证责任推给用户。
- 遇到问题先定位根因，禁止只藏表面现象。
- **严禁过拟合单个 case**：不得因为用户上报了一个具体失败场景，就只为这个场景写硬编码分支或关键词兜底。必须先找到根因，修复后验证同一类问题全部解决，并补能覆盖这一类场景的回归测试。
- **自然语言与能力边界**：Handler / Orchestrator / 权限层不得根据用户原文的关键词、正则或措辞列表决定意图、仓库、工具、任务类型或业务合同；必须由 Agent 输出结构化字段后再做确定性校验。会话已唯一确定当前工作区时，Coordinator 首轮即获得该工作区的有界只读能力（单一根目录、最多 32 次工具调用 / 24 个不同文件），无需人为拆成“先判断、再授权、再读取”三轮。该预算用于允许真实仓库调研和大文件分页，不代表应穷尽仓库；达到证据充分时应立即作答。同一预算失败无信息增益地重复两次后必须停止。仅当会话尚未绑定且存在多个候选仓库时，Coordinator 才先无工具输出结构化 `request_context` 选择一个根目录，下一轮直接在该根目录内只读。跨根目录访问和写操作仍须独立的结构化授权；不得一次接收全部仓库内容。相关架构守卫测试不得为修复单个场景而放宽，确需改变边界须先由用户确认。
- **内部错误不进入用户界面**：schema、provider、堆栈、路径校验等技术错误只写日志；用户界面只能说明是否执行了操作及可采取的下一步。只读 Coordinator 已产出可用自然语言但 JSON 包装失败时，可安全降级为只读答复，不得据此创建或执行动作。
- **自然语言理解边界**：Handler、Orchestrator 和权限层不得根据用户原文中的关键词、同义词表或正则表达式决定意图、仓库、工具能力、任务类型或业务合同。自然语言只由 Agent 转换成结构化字段；确定性代码只校验字段、枚举、权限、路径和状态。显式命令、协议 ID、路径规则、脱敏和展示格式解析不属于自然语言分类。`tests/test_architecture_boundaries.py` 是强制守卫，删除或放宽守卫等同于架构变更，必须先与用户确认。
- **私聊在长任务期间必须可响应**：卡片确认触发 Agent 后不得持有用户消息锁。状态查询、补充/纠正、取消应能在 Agent 运行时进入；Approval / Interaction 自身的原子 claim 和幂等状态负责防重复，不得用会话级长锁代替。
- **反馈按阶段和证据闭环**：反馈关闭不能依赖输入控件或措辞。用户从卡片或自然语言提交的 correction/scope_change/human_rejection，在对应阶段重做并确认后关闭；质量失败只能由 Tester/Reviewer 的结构化证据关闭。`system_observable + manual` 交给 Acceptance Agent，只有偏好、授权和系统无法获取的外部事实交给用户。
- **用户界面不展示内部协议标识**：审批 ID、执行请求 ID、数据库项目 ID、状态机枚举只可存在于卡片 callback value、日志或显式诊断命令中；普通私聊卡片展示项目名和人类可读阶段。临时进度卡完成后应原位替换为实际答复/决策，不得再叠加无信息的“调研完成”卡。
- **Reviewer 规则**：Reviewer Agent 发现 blocking issue（安全漏洞、逻辑错误、功能缺失、与 spec/需求不一致）时，必须设 `blocking: true` 且 `result: "fail"`、`next_state_event: "fail"`。非阻塞建议（代码风格、命名、性能优化）可设 `blocking: false` 不阻塞流程。
