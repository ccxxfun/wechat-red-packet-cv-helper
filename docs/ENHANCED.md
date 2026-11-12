# 指定群红包识别与每日汇总（实验性）

> 仅在本人有权操作的 Windows 10/11 微信桌面窗口上测试。OCR 与模板匹配无法证明红包金额、发送者或是否已领取绝对准确；切勿把输出当成财务凭证。自动化可能违反微信服务条款。本功能默认关闭，必须显式 `--enhanced` 启动。原有命令保持兼容，但不会自动启用群过滤与财务记录。

## 安装与准备

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.enhanced.example.json config.json
```

1. **保持微信窗口可见、固定位置和缩放，手动选中允许的群聊。** 把 `allowed_groups` 和 `selected_group` 改成真实群名；OCR 必须能精确读到选中会话顶部的群名。群名带人数/别名后缀时请按真实可读标题配置；不匹配就不点击。默认不会切换会话。若显式启用 `navigate_group`，须指定 `list_region`，并且当前会话已属于允许群；仅点击列表中 OCR 精确匹配的目标群，点击后重新核对标题。支持 `navigation_scroll_attempts` 限定滚动次数。
2. 按本人屏幕截图填写绝对屏幕坐标 `[左, 上, 宽, 高]`：`header_region`（只有标题，别包含其他文字）、`chat_region`（聊天内容）、`history_region`（可选滚动范围）、`popup_region`（弹层）、`result_region`（金额和发送者截图范围）。示例坐标不可直接使用。`card_status_roi` 则是**相对于匹配到的红包卡片左上角**的区域，必须覆盖“微信红包/已领取”等状态字；若文字不在模板图片匹配范围内，需重新截取更完整的卡片模板。
3. 不同界面截图分别加入 `templates.cards/open/close/claimed` 数组，必须由自己的电脑同一 DPI 截图制成。支持多个模板；已领取、已抢完、已过期文字或已领取模板匹配时跳过。设置 `available_texts` 为你的未领取卡片专有文字，未看清状态时保守跳过。
4. `max_scroll_attempts` 为 0 时不滚动；设置成有限正整数才按 `scroll_direction`（up 或 down）在 `history_region` 内找红包。自动滚动旧消息可能影响读取新消息；建议先用 0 或 1 试验。使用鼠标移到左上角的 PyAutoGUI failsafe，或 Ctrl+C 中断。

首先只扫描不点击：

```powershell
.\.venv\Scripts\python.exe .\grab_hongbao.py --enhanced --config .\config.json --dry-run --scan-once --verbose
```

确认识别后（风险自负）：

```powershell
.\.venv\Scripts\python.exe .\grab_hongbao.py --enhanced --config .\config.json
```

## 文字、金额与推送

成功点击“开”按钮后，在关闭页面**之前**仅对 `result_region` 执行 OCR；需同时唯一识别出金额（¥1.25 或 1.25元）和类似“张三的红包/来自张三的红包”的发送者，才写入记录。界面布局不满足时不会猜测金额/发送人，需据实际截图调整 `extract_packet` 解析规则。无法处理未显示这些信息的界面，也不能从微信客户端取得服务端交易 ID。

`reporting.default_messages` 指定多条默认祝福语，匹配则省略；`removal_phrases` 按字面从文字里删除多条短语；`header`、`footer` 为报表前后自定义话语。`records/YYYY-MM-DD.txt` 按本地系统日期自动更新：逐条发送人、金额和剩余留言、按人汇总、`今日合计功德善款X.XX元`。SQLite 原始记录在 `records/packets.sqlite3`，TXT 是自动生成的快照：**不要直接修改 TXT**，改配置后重新运行，或在 Python 中调用 `DailyReporter(...).write_day(...)` 重新生成。输出涉及敏感个人信息，切勿提交数据库、TXT 或 webhook 到 GitHub；`.gitignore` 已忽略 records 目录。

屏幕自动化无法提供红包唯一交易号。软件对同日相同群、发送者、金额和文字进行保守去重：**两个不同红包但文字金额完全相同可能被当成同一笔**；坐标变化或重启也可能导致重新点卡片。请人工核对每日金额。

机器人地址只从进程环境变量读取，切勿写入仓库；仅允许 HTTPS `qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...`，不跟随重定向。Windows PowerShell 当前窗口示例：

```powershell
$env:WECOM_WEBHOOK_URL = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=换成自己的密钥'
```

把 `push_time` 设为本机时间 `HH:MM`（示例 `00:10`），程序持续运行时到点后会尝试发送**前一天**完整汇总；没有记录时默认不发，可设置 `allow_empty_report=true`。同一天成功发送一次，失败下一轮重试；若发送后又补录旧日期，需人工处理，因为不会自动重发。程序未运行就不会定时发送；需要无人值守请自行配置 Windows 任务计划程序启动脚本（且须登录桌面才可截图）。也可人工指定日期：

```powershell
.\.venv\Scripts\python.exe .\grab_hongbao.py --config .\config.json --send-report 2026-11-11
```

`--dry-run` 不会发送 webhook，不会创建记录。消息涉及第三方姓名与金额，推送前确认已获对方授权。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

自动化测试使用假屏幕/假 OCR/假网络；尚需你提供至少一个真实群的匿名化截图（群标题、未领取卡片、已领取卡片、弹层、金额结果页）校准坐标、模板和文字解析。不要上传私人聊天内容或机器人密钥。
