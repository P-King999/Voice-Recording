# 🛠 进阶使用与配置

返回 [README](../README.md)。

---

## 目录

- [后台服务与日志](#后台服务与日志)
- [启动 / 停止 / 自启](#启动--停止--自启)
- [定时日报与推送渠道](#定时日报与推送渠道)
- [飞书机器人与云文档](#飞书机器人与云文档)
- [用 QQ 邮箱查询历史报告](#用-qq-邮箱查询历史报告)
- [局域网 / 手机访问看板](#局域网--手机访问看板)
- [防休眠与合盖继续录音](#防休眠与合盖继续录音)
- [常用配置项](#常用配置项)
- [故障排查](#故障排查)

---

## 后台服务与日志

一个 `agent.py` 进程守护全部子服务，任何服务异常退出都会按退避策略重启（十分钟内连续失败超过 5 次会停止重启并记录日志）。

| 服务 | 作用 |
|---|---|
| `recorder.py` | 录音、切片、静音判断、关机遗留文件修复 |
| `worker.py` | 转写、对话分析、静音过滤后的归档 |
| `summary_scheduler.py` | 上午/下午/晚上/本日/本周汇总，以及无内容录音清理 |
| `reporter.py` | 定时日报（邮件 / 飞书 / 企业微信 / PushPlus） |
| `mail_query.py` | 用邮箱向自己发信，查询指定日期的报告 |
| `dashboard.py` | Streamlit 看板（默认 8501 端口） |
| `keep_awake.py` | 阻止系统因空闲自动睡眠 |
| `power_manager.py` | 电池供电时自动切换节能电源计划 |

日志都在 `runtime/`：`agent.log`、`recorder.log`、`worker.log`、`reporter.log`、`summary_scheduler.log`，按天轮换。

---

## 启动 / 停止 / 自启

```powershell
# 后台启动（不弹窗口）
.\Start-VoiceRecorder.ps1        # 或双击 Launch-VoiceRecorder.vbs

# 配置登录自启（任务计划 + 注册表双保险）
.\Install-AutoStart.ps1

# 取消自启
.\Uninstall-AutoStart.ps1

# 完全停止所有后台服务
.\Stop-VoiceRecorder.ps1
```

> `Stop-VoiceRecorder.ps1` 只停止本项目启动的进程，不会影响其他 Python 程序。
> 如果启用了自启，停之前先执行 `Uninstall-AutoStart.ps1`，否则下次登录又会启动。

---

## 定时日报与推送渠道

在 `.env` 里开启：

```ini
REPORT_ENABLED=true
REPORT_TIMES=12:10,17:10        # 每天几点发（用你电脑常开的时间）
REPORT_CHANNELS=smtp            # smtp / feishu / wecom / pushplus，可逗号组合
```

报告的范围是**上一份报告结束 → 本次触发时刻**的连续区间；关机错过的时段会在下次开机时合并成一份「补发」报告，不会静默丢失。

**QQ 邮箱 SMTP**（默认渠道）：

```ini
REPORT_EMAIL_RECIPIENT=you@qq.com
REPORT_SMTP_USERNAME=you@qq.com
REPORT_SMTP_AUTH_CODE=xxxxxxx    # QQ 邮箱的「SMTP 授权码」，不是登录密码
```

**企业微信机器人**：`REPORT_CHANNELS=wecom`，填 `REPORT_WECOM_WEBHOOK`。
**PushPlus**：`REPORT_CHANNELS=pushplus`，填 `REPORT_PUSHPLUS_TOKEN`。
个人微信没有稳定的官方机器人接口，不直接支持。

---

## 飞书机器人与云文档

1. 在飞书开放平台建一个自建应用，开启「以应用身份发消息」「读取群聊」「创建/编辑云文档」「多维表格」权限
2. 把应用拉进一个群，记下群名
3. 配置：

```ini
REPORT_CHANNELS=smtp,feishu
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_CHAT_NAME=录音日报
```

发送连接测试（不会生成摘要，也不发其他渠道）：

```powershell
.\.venv\Scripts\python.exe reporter.py --send-feishu-test
```

可选：把录音元数据同步到多维表格

```ini
FEISHU_BITABLE_APP_TOKEN=xxx
FEISHU_BITABLE_TABLE_ID=tblxxx
```

```powershell
.\.venv\Scripts\python.exe reporter.py --sync-feishu-archive
```

---

## 用 QQ 邮箱查询历史报告

`mail_query.py` 会定期检查收件箱（默认 60 秒一次）。用配置的同一个 QQ 邮箱**给自己发一封邮件**：

- 主题/正文随便写 → 回复当前报告
- 写 `2026-08-12` 或 `2026年8月12日` → 回复那一天的报告

只会处理同一邮箱发来的新邮件，并跳过系统日报，避免死循环。

---

## 局域网 / 手机访问看板

1. 看板「设置 → 允许局域网访问看板」打开（监听地址会变成 `0.0.0.0`，保存后自动重启看板）
2. 首次开启需要**管理员权限**执行一次防火墙放行：

```powershell
.\Enable-DashboardLAN.ps1
```

3. 手机连同一个 Wi-Fi，访问 `http://<电脑的 WLAN IPv4>:8501`（`ipconfig` 查看）

> 一旦开启局域网访问，系统会强制要求密码登录（本机免密码失效）。
> 如果 Wi-Fi 被 Windows 标记为「公用网络」，先改成「专用网络」。

---

## 防休眠与合盖继续录音

`keep_awake.py` 只阻止**空闲自动睡眠**，不会点亮屏幕。合盖是单独的电源策略：

**控制面板 → 电源选项 → 选择关闭盖子的功能 → 接通电源时：不采取任何操作**

禁用插电时的空闲睡眠超时（可选）：

```powershell
.\Configure-LaptopPower.ps1
```

不会修改电池供电下的睡眠设置。项目不会配置自动关机 / 重启 / 注销。

---

## 常用配置项

`.env` 里最常改的几个（改完重启服务生效）：

| 键 | 默认 | 说明 |
|---|---|---|
| `RECORDING_DEVICE` | `auto` | 麦克风名称；`auto` 会跳过虚拟声卡自动选真实麦克风 |
| `SEGMENT_SECONDS` | `3600` | 每段录音秒数，1 小时 = 3600 |
| `SILENCE_THRESHOLD_DBFS` | `-48` | 静音判定阈值，数值越大越容易判静音 |
| `MIN_ACTIVE_SECONDS` | `3` | 一段里至少要有多少秒人声才算「有内容」 |
| `DEFAULT_SUMMARY_MODE` | `deepseek_flash` | 默认汇总模型，会自动跟随 DeepSeek 最新版本 |
| `EMPTY_RETENTION_DAYS` | `30` | 无内容录音保留天数，到期移入回收站；0 = 不自动清理 |
| `RECOVER_PARTIAL_DAYS` | `7` | 启动时自动修复多少天内遗留的 `.part.wav` |
| `DASHBOARD_PASSWORD` | 空 | 看板密码，首次打开会引导设置 |

模型列表每 12 小时从 DeepSeek `/models` 自动同步，模型改名不需要改代码。

---

## 故障排查

| 现象 | 先看这里 |
|---|---|
| 一直静音 / 没有转写 | 设置里点「测试麦克风」，确认不是虚拟声卡；看 `runtime/recorder.log` 有没有 `input overflow` |
| 转写卡在「等待转写配置」 | 百炼 Key 没配或没开通语音转写 |
| 看板 502 / 打不开 | `runtime/dashboard.error.log`；或 `.\Start-VoiceRecorder.ps1` 重新拉起 |
| 日报没收到 | `runtime/reporter.log` 搜 `Delivered` / `Could not deliver` |
| 某个服务反复重启 | `runtime/agent.log` 搜 `Disabled`，说明连续失败太多，修好原因后再启动 |
| 录音文件是 0 秒 | 旧版本残留的 `.part.wav`，现在启动时自动修复；也可手动 `python recorder.py --recover-partials` |
