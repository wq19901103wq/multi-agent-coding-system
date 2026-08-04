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
2. `/whoami` 确认事件接收；
3. `/current` 确认会话项目与目录；
4. 查看 RootAgentSession 的 thread id 和 execution status；
5. 检查 NotificationOutbox/CardProjection 是否与飞书卡片一致；
6. app-server 协议异常再查看 Root Agent event，不用旧状态机字段推断。

测试时不要把 `FEISHU_TEST_MODE=1` 写进 `.env`；只在单次 E2E 命令环境中临时设置。
