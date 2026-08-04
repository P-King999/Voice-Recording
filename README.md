# 持续录音与智能整理

Windows 笔记本用的后台录音系统。它持续录制麦克风，每小时自动切成独立文件；本地过滤静音后，将有声音的片段上传到阿里云百炼临时文件区进行异步转写。无需 OSS。转写完成后，百炼临时文件会被删除，本地音频、逐字稿和分析结果会保留。

## 功能

- 连续录音并按固定时长切片，默认每 1 小时，可自行改为 5 分钟、10 分钟等。
- 本地静音过滤，安静片段不发送到云端。
- 百炼文件转写：本地 MP3 直传百炼临时文件区，自动使用短期签名地址提交转写，不产生 OSS 存储配置和费用。
- 阿里云语音分析：请求说话人分离与声音事件检测；云端明确返回的性别、语种、事件字段会显示在页面。系统不会自行猜测。
- 分时段总结：今天、今天上午、本周、本月。
- 总结模型可在页面即时切换：默认 `DeepSeek-V4-Flash`，可改为 `DeepSeek-V4-Pro` 或 `千问 Qwen Plus`。默认模型不可用时，依次回退到 Pro 与千问。
- 全部服务后台运行，带登录自启与防自动休眠。

## 从零部署

### 1. 准备环境

1. 安装 Windows 10/11 与 Python 3.11 或更高版本。安装 Python 时勾选 **Add Python to PATH**。
2. 在 GitHub 私有仓库页面点击 **Code**，复制 HTTPS 地址，然后在 PowerShell 执行：

```powershell
git clone https://github.com/P-King999/Voice-Recording.git
cd Voice-Recording
```

3. 创建虚拟环境并安装依赖：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. 配置密钥

在项目根目录创建 `.env`，可以复制样例：

```powershell
Copy-Item .env.example .env
```

填写或放置下列密钥，二选一即可：

- 百炼：将百炼业务空间下载的 `*apiKey*.csv` 放在项目根目录，程序自动读取其中的 `apiKey` 与 `workspaceId`；也可以在 `.env` 填写 `DASHSCOPE_API_KEY` 与 `DASHSCOPE_WORKSPACE`。
- DeepSeek：将 API Key 单独放入根目录 `deepseekkey.txt`，或在 `.env` 填写 `LLM_API_KEY`。

`.env`、CSV 和 `*key*.txt` 都已被 Git 忽略，绝不能提交、复制到公开位置或发送给他人。配置好后，可在 `.env` 调整每段时长：`SEGMENT_SECONDS=3600` 表示 1 小时。

### 3. 允许麦克风

进入 Windows **设置 -> 隐私和安全性 -> 麦克风**，打开“麦克风访问权限”和“允许桌面应用访问麦克风”。不确定默认麦克风是否正确时，先在系统“声音 -> 输入”中测试。

### 4. 后台启动

双击 `Launch-VoiceRecorder.vbs`。它不会打开命令行窗口，而是在后台启动四个服务：录音、处理、阻止自动休眠、网页面板。

打开浏览器访问 `http://localhost:8501`，或双击 `Open-Dashboard.cmd`。页面只负责展示，关闭浏览器不会停止录音。

### 4.1 在手机上查看（局域网）

首次开启局域网访问时，以管理员身份打开 PowerShell 并在项目目录执行：

```powershell
.\Enable-DashboardLAN.ps1
```

确保手机和这台电脑连接到同一个 Wi-Fi，然后在手机浏览器输入电脑的 WLAN IPv4 地址和端口，例如 `http://10.105.45.63:8501`。可用 `ipconfig` 查看当前 WLAN 的 IPv4 地址；它在网络变化后可能改变。

该防火墙规则只允许专用（Private）网络访问 TCP `8501`。如果当前 Wi-Fi 被 Windows 标记为公用网络，请先在 Windows 网络设置中将该 Wi-Fi 改为专用网络。

### 5. 开机后自动启动（可选）

以普通用户身份在 PowerShell 执行：

```powershell
.\Install-AutoStart.ps1
```

该命令只为当前 Windows 用户添加登录自启。若不再需要，执行：

```powershell
.\Uninstall-AutoStart.ps1
```

## 防休眠与合盖

后台的 `keep_awake.py` 会阻止系统因空闲自动睡眠，但不会强制点亮屏幕。笔记本合盖是单独的电源策略：希望合盖后继续录音时，在 Windows **控制面板 -> 电源选项 -> 选择关闭盖子的功能** 中，将“接通电源时”设为“不采取任何操作”。

如需禁用插电时的空闲睡眠超时，可执行：

```powershell
.\Configure-LaptopPower.ps1
```

这不会修改电池供电时的睡眠设置。

## 如何停止

要完全停止录音、转写、网页和防休眠服务，在项目根目录的 PowerShell 执行：

```powershell
.\Stop-VoiceRecorder.ps1
```

该命令只停止本项目启动的后台进程，不会影响其他 Python 程序。若还启用了登录自启，请另行执行 `Uninstall-AutoStart.ps1`，否则下次登录会再次启动。

## 数据位置

- `data/recordings`：有声音的 MP3 和等待处理的音频。
- `data/silent`：本地静音过滤的文件。
- `data/failed`：多次处理失败的音频。
- `data/voice_recorder.sqlite3`：片段、逐字稿和时段分析。
- `runtime`：后台日志与进程 PID 文件。

这些目录均不上传 GitHub。录音涉及他人隐私和当地法律要求，请在录音前取得适当授权。
