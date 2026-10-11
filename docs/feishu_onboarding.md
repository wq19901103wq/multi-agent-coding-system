# 飞书接入

MAC 使用飞书 `lark-oapi` WebSocket 长连接接收私聊、群聊和卡片事件，不需要公网回调。

## 配置

`.env` 至少包含：

```env
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_TRANSPORT=websocket
MCS_PORT=18080
```

多个机器人账号可以同时挂到同一个 MAC 服务：

```env
FEISHU_BOT_ACCOUNTS_JSON='{"default":{"app_id":"cli_xxx","app_secret":"xxx"},"reviewer":{"app_id":"cli_yyy","app_secret":"yyy"}}'
FEISHU_DEFAULT_BOT_ACCOUNT_ID=default
```

旧的 `FEISHU_APP_ID`/`FEISHU_APP_SECRET` 仍作为默认账号兼容。每个项目固定归属一个
`bot_account_id`；聊天选择、事件去重、Owner 记忆命名空间和通知发送都按账号隔离。不同机器人可以
像多个 Codex 会话一样打开同一工作目录，各自保留独立 Project Lead Thread；目录本身是显式共享的
工作区，并发修改风险由 Git/worktree 边界处理。由于 `lark-oapi` 1.x 的 WebSocket event loop 是模块级
全局状态，MAC 为每个机器人启动一个隔离 worker 进程，但业务模型仍是同一套 Project Lead 生命周期。

飞书应用需要启用机器人、发布版本，并订阅 `im.message.receive_v1` 和
`card.action.trigger`。常用权限包括消息读取和 `im:message:send_as_bot`。

## 运行

```bash
./scripts/start_server.sh
```

检查端口、管理页和最新日志：

```bash
lsof -Pi :18080 -sTCP:LISTEN
curl http://127.0.0.1:18080/admin/projects
ls -lt server_*.log | head
```

禁止用未限制目录的 `uvicorn --reload`。项目内 Agent 写文件会触发服务反复重启。

## 消息路径

```text
飞书事件
  → ws_client（验签、去重、短生命周期回调）
  → handlers（命令、项目解析、卡片动作）
  → Root Agent service loop（长生命周期 Codex turn）
  → app-server 原生事件
  → NotificationOutbox / CardProjection
  → 飞书卡片创建或原地更新
```

运行中的项目消息优先 `turn/steer`；Root idle 时异步启动新 turn，不能阻塞飞书回调等待模型
完成。原生 `request_user_input` 创建问题卡，答案返回同一个 app-server request。

## 调试顺序

1. 日志确认 WebSocket connected；
2. `/help` 确认事件接收和当前可用命令；
3. `/info` 确认当前项目与真实工作目录；
4. 查看 RootAgentSession 的 thread id 和 execution status；
5. 检查 NotificationOutbox/CardProjection 是否与飞书卡片一致；
6. app-server 协议异常再查看 Root Agent event，不用旧状态机字段推断。

测试时不要把 `FEISHU_TEST_MODE=1` 写进 `.env`；只在单次 E2E 命令环境中临时设置。
