# MAC System Roadmap

## P0：当前承诺闭环

- [x] 完整单元测试与 lint 全绿（320 passed，2026-07-20；历史文件格式化债务另计）。
- [x] 真实飞书 `conversation_delivery`：讨论 → 执行确认 → 单次创建 → 重复点击回执。
- [x] 真实飞书规划驳回输入卡、统一 ask_user、Acceptance ask_user 和恢复卡渲染验证（2026-07-16/17，四个场景均通过）。
- [x] 同一真实飞书 full-flow 项目覆盖全回退分支并到 `COMPLETED`（最新复验：2026-07-20，`proj_20260719_164305_f9de`）。
- [x] `full_feature_flow` 脚本自动处理质量 Agent 主动 fail 的 REWORK（2026-07-20 真实 Tester 主动 fail → `to_testing` → 重测通过）。
- [ ] SQLite WAL/busy-timeout 并发保护的压力验证。
- [x] 项目取消/终止信号主动中断已启动的 Claude CLI 子进程，`active_task_cancel` 真实飞书场景通过（2026-07-20，13.3 秒）。

## P1：移动端产品体验

- [ ] 持续更新的项目状态卡。
- [ ] 通知降噪和“稍后处理/接受推荐/全部默认”快捷操作。
- [ ] 管理后台提供恢复、接管、重试和归档。
- [ ] 用真实历史对话建立 Coordinator 路由评测集。

## P2：生产化

- [ ] API/Worker 分离与持久任务队列、lease、heartbeat。
- [ ] PostgreSQL、Alembic、备份、保留与清理策略。
- [ ] 完整 RBAC、安全沙箱、审计、成本预算与模型降级。
- [ ] 指标、追踪、告警、部署与升级流程。
