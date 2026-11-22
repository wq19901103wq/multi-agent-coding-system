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
接收文件和照片还需开启消息资源读取权限 `im:resource`，并发布应用版本使权限生效。

在私聊中直接发送文件、照片，或包含图片的图文消息，即可交给当前项目助手读取；
运行中发送会追加到当前任务。首次发送附件会创建项目。附件按项目保存在
工作目录的 `.mac/attachments/` 下，同名文件互不覆盖，每个附件最大 30 MB。
可在发送前后补充处理要求。群聊仍需 @机器人；下载失败会显示原因卡片。
可以多选文件发送（客户端可能拆成多条消息），或将多份文件合并转发给机器人。
合并转发按原消息读取文件，最多展开 3 层、50 条消息；超限或不支持的内容会提示。
PDF、Word、Excel 可以混合发送；同一批中下载失败的文件会逐份提示，其余文件继续处理。
支持读取 PDF、Word（DOCX/DOC/RTF）、Excel（XLSX/XLSM/XLS）和常见文本文件
（TXT、CSV、TSV、Markdown、JSON 等）。附件上下文会附上本机读取命令，
助手通过普通命令工具按需读取，不会预先把整个文件塞进对话。

也可以直接提供助手有权限读取的本地文件路径，并说明处理要求。命令行用法：

```bash
.venv/bin/python -m mac_system.local_files "/本地路径/报告.pdf" --start 1 --limit 5
.venv/bin/mac-read-file "/本地路径/数据.xlsx" --sheet "销售" --start 1 --limit 50
.venv/bin/mac-read-file "/本地路径/报告.pdf" "/本地路径/数据.xlsx" "/本地路径/说明.docx"
```

输出 JSON 保留页码、正文块或行号，并标记截断与继续位置。
多个路径会输出 `files` 列表和 `succeeded`、`failed` 数量；单个文件解析失败不影响其他文件，
有失败时退出码为 1，成功结果仍在标准输出中。一次最多读取 50 个文件，
`--max-chars` 默认 24000，为整批正文的总字符预算，均分给各文件。
截断时可按该文件的 `next_start` 单独继续读取；分页参数应用于整批，
需要不同页码或工作表时请分别运行。单文件命令保留原有输出结构。
Excel 默认读取首个工作表，同时列出全部工作表；不执行宏、公式或外部链接，
XLSX/XLSM 保留公式原文，XLS 读取已保存的结果。
扫描 PDF 没有文字时会提示需要 OCR 或图片工具；加密文件需提供已解密副本。
Word 读取正文和表格，不包含图片、文本框、页眉页脚及批注；旧版 DOC/RTF
依赖 macOS 自带的 `textutil`，转换时不保留版式。

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
