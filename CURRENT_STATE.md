# MAC 当前状态

更新时间：2026-08-04（Asia/Shanghai）

## 正在重构

当前分支：`codex/rebuild-core`。

已完成：

- 旧现场已归档到本地分支 `codex/archive-pre-rebuild-20260803`，提交 `0554147`；
- 执行内核改为持久 Codex Root Agent Thread；
- Root 第一轮恢复正常 workspace-write、Shell、apply_patch、skills 和 Codex 配置；
- 启用 Codex 原生 multi-agent v2，不再由 MAC 重写 spawn/wait/followup；
- 原生 `request_user_input` 已映射到持久问题卡并可返回原 turn；
- 项目创建不再生成固定 Python 骨架、任务类型流程或规划产物；
- Root 结果和原生 Child Agent 活动已接入飞书卡片投影；
- Root runtime/persistence 定向测试 11 个通过；API/飞书回归 245 个通过。

尚未完成，因此本分支还不能作为交付版本：

- 删除旧 `AgentOrchestrator` 状态机实现及其 Prompt/Schema/profile；
- 完成 Claude Code、Kimi、Pi 的 Root 工具适配；
- 更新 E2E 脚本为 Root turn 语义；
- 全量测试；
- 真实 Codex app-server、真实飞书私聊 UI 和个人 Agent 项目黑盒。

只有上述验证全部通过后才更新本文件为“可用”。
