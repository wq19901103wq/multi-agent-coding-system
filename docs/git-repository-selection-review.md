# `/ls`、`/use` Git 仓库语义复核记录

日期：2026-10-12。范围仅为“已有 Git 仓库可被发现并通过 `/use` 打开”的本轮改动。
本记录由当前执行者进行第二轮代码与证据交叉核验，不代表另外的评审 Agent、外部 CLI
或第三方独立审查。继承的测试结果与本轮新执行的核验分开记录。

## 结论与边界

本次复核未发现阻断本轮实现的缺陷。实现符合当前聊天项目与允许范围内 Git 仓库共同
选择的语义，保留目录授权、歧义选择和聊天 / 机器人隔离；不新增执行控制循环。

本轮未修改实现或仓库内回归测试，仅补充本记录及临时目录中的离线核验材料。
未 editable reinstall、重启 / 停止服务、修改配置或生产数据库、执行线上验收，
也未打断现有任务。当前运行服务仍未部署本次改动。

“有 Git 就能 use”仍受 `MAC_ALLOWED_BIND_ROOTS` 授权范围约束；列表默认只发现根目录
及两层子目录。深层仓库可用绝对路径或 `~/` 打开，不承诺自动扫描整个文件系统。

## 代码与既有回归交叉核对

| 核对项 | 代码依据 | 已有测试依据 |
| --- | --- | --- |
| 发现未登记仓库，不被原默认 12 项截断；已登记目录不重复 | `feishu/handlers.py:1530`、`core/project_service.py:405` | `test_ls_discovers_repositories_without_local_projects_or_writes`、`test_use_search_is_not_limited_to_scanner_default_or_first_page`、`test_scan_candidate_repositories_full_discovery_is_sorted_and_deduplicated` |
| 精确当前聊天项目优先于仓库名，精确仓库名优先于项目名前缀；歧义不猜测 | `feishu/handlers.py:1560`、`feishu/handlers.py:1634` | `test_use_exact_registered_name_precedes_repository_name`、`test_use_exact_repository_precedes_project_prefix`、同名仓库 / 混合前缀歧义测试 |
| 选择卡完整分页，翻页保留筛选；路径可区分同名项 | `feishu/handlers.py:1609`、`feishu/menu.py:69` | `test_use_pagination_keeps_all_candidates_and_freezes_old_card`、`test_use_ambiguous_pagination_preserves_search_filter` |
| 深层已有仓库可直接打开；不存在、非 Git、损坏 Git、子目录和越界符号链接被拒绝 | `feishu/handlers.py:1634`、`core/project_service.py:369`、`config.py:161` | `test_use_absolute_repository_beyond_discovery_depth`、`test_use_invalid_paths_do_not_initialize_repository_or_project` |
| 同聊天复用项目与 native thread；其他聊天 / 机器人不导入历史 | `feishu/handlers.py:1786`、`core/project_service.py:693` | `test_use_repository_reuses_local_project_and_thread`、`test_use_shared_repository_never_imports_another_chat_or_bots_thread` |
| 列表与切换不启动 coding Turn；旧卡不跨时间原地改写 | `feishu/handlers.py:1578`、`feishu/handlers.py:1786`、`feishu/handlers.py:391` | 仓库选择测试的 `scheduled == []` 断言、翻页冻结旧卡测试 |

代码路径均相对 `src/mac_system/`；测试定义位于 `tests/test_feishu.py:1343` 起及
`tests/test_project_service.py:2070` 起。原生 thread / turn / steer 协议实现未在本次 diff
中改动；目录打开仍复用原 `/open`，不是另造 session 或恢复流程。

扫描仅按 `.git` 文件 / 目录存在发现候选，真正打开时再次验证允许路径和 Git 根目录。
无效候选不会因点击而创建目录或初始化仓库。已有有效空仓库仍沿用原 `/open` 的
初始提交行为，因此不能把所有 `/use` 操作概括成“完全只读”。

## 复用上轮证据

以下根据上轮已完成验证记录复用，本轮没有重新执行整套测试：

- T1：`.worktrees/feat-core/.venv/bin/python -m compileall -q src tests` 通过。
- T2–T3：完整 `pytest tests/ -q` 为 **741 passed、3 warnings，355 秒**；
  沙箱外获批运行的已完成 session 为 `98526`。
- 改动相关定向回归为 **23 passed、121 deselected**。
- 上轮只读实际仓库扫描发现 19 个候选，包含 `digital-human` 和
  `digital-human-framework`；本轮没有重新扫描用户目录。
- 上轮沙箱测试的 macOS `sysctl` 权限失败已由沙箱外完整通过结果消除；不将其算作
  产品缺陷，也不将另一个旧版本评审中的测试数量移植到本轮。

## 本轮新执行的补充核验

为补足调用边界与真实 Git worktree 的证据，使用现有 `test_feishu` 的 RecordingBridge、
内存 SQLite 和 Fake Orchestrator 夹具，只在 `/private/tmp/mac-repository-review.bSIoAS/`
内创建测试仓库，不连接飞书、provider 或运行服务。

- 过期候选按钮的三种情形：目录移走、`.git` 移走、原路径改为指向授权范围外的
  符号链接。点击均显示“打开失败”，不产生新项目 / 目录，不改变原选择、模拟 running
  状态、native thread 或 active turn，也不发起 schedule / cancel / stop。
- 使用真实 `git worktree add` 创建 `.git` 为文件的 worktree。`/ls` 能发现它，
  `/use` 能打开；Git marker、HEAD 和干净工作树保持不变，不启动 coding Turn。
- 修正后的完整补充结果为 **4 passed、2 warnings，7.46 秒**，两项 warning 均来自
  既有 `jsonschema.RefResolver` 弃用提示。这四项属于离线 T2–T3，不属于 T4 或 T5。
- 初次补充运行曾出现三项失败：脚本先设置 running，再调用 `set_native_thread`，
  后者按既有语义将状态设为 idle。调整脚本为先设置 thread，再通过 `set_turn` 设置
  模拟 running 后重跑全部四项通过；没有为此修改产品代码。
- 本轮 `git diff --check` 通过。

执行命令：

```bash
.worktrees/feat-core/.venv/bin/python -m pytest -q -p no:cacheprovider \
  --basetemp=/private/tmp/mac-repository-review.bSIoAS/fixtures-rerun \
  /private/tmp/mac-repository-review.bSIoAS/test_repository_review.py
```

结果保存在 `/private/tmp/mac-repository-review.bSIoAS/supplemental-results.txt`。
临时文件可能被系统后续清理；本记录保留结果、输入摘要和重跑命令，不承诺永久归档。

## 输入标识

复核时 Git HEAD 为 `dcfd0dedbd2453cf74296943df8c06a7cb6c7757`；本轮未提交。
下列 SHA-256 标识复核输入，不将仅有的文件摘要当作运行服务验收。

```text
99b4e5ad449fbbeab37be7ef786c66e5ca67bb86a8b0c5e7b0473f16e9075c45  README.md
de25e2b4c01e113e0540e9c7eaf69d7e31498e9c8fe309acf63e71f4430f00fd  docs/interaction-spec.md
cd1b74e83f5ff5fa329f26a5f3462b9a474fa7ce7b496d6f6eb54f1e0ed3b0a0  src/mac_system/core/project_service.py
b4be5f5b94a3574f5bf4cba201cc7fff5b7de94a3251de720a7972b657eeff66  src/mac_system/feishu/handlers.py
9cc63052e27a89cde761b9b5d3b5c924e11bbf23860d8bd11652e31fc805e912  src/mac_system/feishu/menu.py
ffe5b4e420076829056381e81fb50680f9229c15c00f27e8834a2b335c63559a  tests/test_feishu.py
f94bc80e97f75885eeaf28d15dec77d34887ad610344a1c8ac12af522ac76a0c  tests/test_project_service.py
8a069ca0410b248c0ea1cfa730a555f7575c400b7db21ed724c16cb194851ebd  test_repository_review.py
4ab7f0aba6ac2184bce0562e480d6876594d836167cf72ffa0144d37864cd003  supplemental-results.txt
```

## 未执行与收尾

未执行本次改动的安装 / 重启、T4 真实服务验收、T5 飞书 UI 黑盒或 T6–T9。
Fake Orchestrator 的模拟 running 断言不能证明真实在线任务的运行行为；本轮不接触
真实任务，因此也不以其替代线上验收。保持“暂不上线”，本次仅补足离线复核依据后收尾。
