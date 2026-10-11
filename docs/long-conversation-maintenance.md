# 长对话修复的一次性维护

本次部署修复使用 Codex 原生上下文压缩、轻量 `thread/resume`
（`excludeTurns=true`）、较大的协议读取缓冲及启动等待预算。
维护不创建替代 thread，不删除项目、历史或待办，不把部署成功当作验收通过。

## 执行前提

- 用户已允许短暂、可恢复的维护；已有任务必须自然结束。
- Root 已 idle 不代表原生后台终端已结束。先只读核对 MAC 服务的进程与
  创建时间，以 `observers` 身份列表连同初始空 `processes`、`groups` 保存为
  witness JSON，通过 `--wait-execution <path>` 传入。脚本持续捕捉服务及
  已记录执行的子进程和独立进程组，包括等待期间新出现的后台任务。
  app-server、multiprocessing 基础设施自身及 CUA 工具不作为执行任务，
  但不排除整棵 app-server 执行树。脚本只检查是否退出，
  不发送停止信号；身份或权限无法核实时阻塞维护，不能仅凭数据库 idle 重启。
- 先执行 T1–T3，然后在项目虚拟环境内 `pip install -e . --no-deps`。
- 安排维护的 Root 自身也是活动任务，维护进程必须由独立的一次性
  launchd job 运行，不能附属于 MAC 服务；只设 `RunAtLoad`，不设 `KeepAlive`。
- 旧服务没有原子的维护入口锁。脚本会等待连续 30 秒无执行、审阅或投递，
  并在重启前复核，但不能消除复核与停止之间的新任务竞态。因此只在用户
  同意暂不提交新任务的短暂维护窗口执行；不要宣称已经实现全局入口封锁。

## 操作

使用固定且唯一的授权操作 ID：

```bash
.worktrees/feat-core/.venv/bin/python scripts/deploy_long_conversation_once.py \
  run long-conversation-20261011-resume --wait-ready --timeout 300 \
  --notify-project <project_id> --wait-execution <witness_path>
```

- `--wait-ready` 先无截止时间地被动等待自然结束，不更改任何 Owner 开关，
  每 30 秒只读观察；进入连续无阻塞确认后每 10 秒检查。
  此阶段不会因原先的两小时等待到期而丢失接续，也不会停止现有任务。
  `readiness.json` 持久记录后台任务身份，`status.json` 仅在阻塞数量变化时更新。
- 就绪后才进入已同意的短暂维护窗口；`--timeout 300` 只限制这一窗口，
  不限制被动等待。就绪后的维护等待及最终停服复核仍刷新后台任务 witness。
- 写入私有快照并落盘后，临时暂缓原本启用的 Owner 自动续推。
  这不是用户的 `/owner off`，不批量取消现有审阅；运行中的任务不被打断。
- 旧数据库可能保留已退出活跃模型的必填 Owner 字段。仅为原本没有开关行的
  项目创建临时行时，将这三个旧文本字段留空以兼容约束；不迁移表、不改写
  已有旧字段，恢复时删除本次创建的临时行，保留期间的真人开关选择。
- 等所有执行、审阅和投递结束；维护开始时活动轮次的结果必须全部送达，
  分页齐全并有飞书消息 ID，才允许重启。失败轮次没有结果时会保守超时。
- 调用既有 `restart_server_once.sh`，同一操作最多尝试一次。
  超时、新工作或失败都不能自动换 ID 重试。
- 正常结束、异常或 TERM/INT 均比较并恢复 Owner 原状态，不覆盖期间的
  真人开关。完整送达但因暂缓而遗漏的当前轮审阅恢复到队列，不重发结果；
  已被新真人指令、新轮次或重置淘汰的旧审阅不重新推进。
- 保存停服前的 thread 标识并核对重启后标识；不一致时只报告异常，
  不改回旧标识、不再重启。恢复通知经现有 outbox 发送。
- 恢复通知必须 `sent` 且有真实飞书消息 ID 后，才将实际卡片提交给现有
  Owner 持久队列，审阅 ID 使用 `maintenance:<operation_id>:acceptance` 幂等键。
  `owner_audit` 审阅不绑定伪造的原生轮次、不写入真人历史轮；Owner 根据
  真人主线继续同一 Root 完成验收。通知失败不会假称已唤醒。
  `acceptance_dispatch` 区分等待送达、已入队、未派发；进程中断不派发。
  任一维护失败均不自动重试重启。

记录位于 `.mac/long_conversation_maintenance/<operation_id>/`。
`status.json` 的 `restarted_pending_acceptance` 仅表示重启及标识核对成功。
维护 job 结束后可卸载该 job，不卸载 MAC 主服务。

## 异常恢复与验收

SIGKILL、宿主断电或恢复步骤本身失败时，先核对 `status.json`、维护进程、
服务和 `.mac/restart_operations/`，不要重跑 `run`。确认维护进程已退出后：

```bash
.worktrees/feat-core/.venv/bin/python scripts/deploy_long_conversation_once.py \
  restore <operation_id>
```

`restore` 只恢复 Owner 和当前轮缺失的审阅；维护 PID 仍存在时拒绝争抢。
它不停止或启动服务。进程权限不足、PID 被复用时也应先人工核查。

验收独立于部署：T4 核对新 PID、18080、`/admin/projects` 200、各飞书
WebSocket、原 thread、Owner 恢复及项目继续推进；T6 核对原长对话真实续接。
T5 必须通过已登录飞书界面查看本轮结果卡、进行真实对话续接，且先取得
界面控制及测试消息发送所需的权限。HTTP 200、数据库 `sent` 和测试注入
均不能替代 T5；真实验收完成前不得宣布“好了”。
