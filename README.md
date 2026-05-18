# Bike_Thinker

抓取腾讯文档「骓特C6」并整理为列表形式的 Python 脚本。

## 工作原理

脚本使用 [Playwright](https://playwright.dev/) 启动浏览器并复用本机
Edge / Chrome 的腾讯文档登录态。提供 3 种模式：

| 模式 (`--mode`) | 说明 | 是否需要先关掉本机 Edge/Chrome |
| --- | --- | --- |
| `clone` (默认) | 把登录所需的 Cookies / Local State 等少量文件复制到 `%TEMP%\bike_thinker_browser_profile`，Playwright 用这份独立 profile 启动浏览器。 | **不需要**，可以和你正在用的浏览器并存 |
| `share` | 直接把本机 Edge/Chrome 的 user-data-dir 借给 Playwright（官方做法）。 | **需要**，且要彻底结束所有 `msedge.exe` 后台进程，否则 `exitCode=21 (PROFILE_IN_USE)` |
| `attach` | 连接到你自己手动启动的、开了 `--remote-debugging-port` 的浏览器。 | 不需要，但要自己启动调试浏览器 |

## 使用步骤（Windows / PowerShell）

```powershell
cd D:\CUSOR_WS\Bike_Thinker

# 1. 安装依赖
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install

# 2. 运行（默认 clone 模式，最省事）
python fetch_zhuite_c6.py
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--browser {edge,chrome}` | 选择复用哪个浏览器的登录态，默认 `edge` |
| `--profile "Profile 1"` | 浏览器 profile 目录名，默认 `Default` |
| `--user-data-dir <PATH>` | 显式指定浏览器 user-data-dir 路径 |
| `--mode {clone,share,attach}` | 登录态复用方式，默认 `clone` |
| `--cdp-port 9222` | `attach` 模式连接的远程调试端口 |
| `--headed` | 显示浏览器界面（排查问题时用） |
| `--out <DIR>` | 输出目录，默认当前目录 |

### attach 模式步骤

如果 clone 模式出问题，可以手动起一个调试 Edge：

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
    --remote-debugging-port=9222 `
    --user-data-dir="$env:LOCALAPPDATA\Microsoft\Edge\User Data"
```

在弹出来的这个 Edge 里登录 https://docs.qq.com（如果还没登录），然后在另一个终端：

```powershell
python fetch_zhuite_c6.py --mode attach --cdp-port 9222
```

## 输出

* `zhuite_c6.json` — 结构化结果（在线文档为段落列表，在线表格为二维数组）。
* `zhuite_c6.csv`  — 若为在线表格则同时输出 CSV。

控制台会打印前 5 行 / 段作为预览。

## 常见报错

### `BrowserType.launch_persistent_context: Target page, context or browser has been closed`，并伴随 `exitCode=21`

原因：Edge / Chrome 的 user-data-dir 被另一个 msedge / chrome 进程占用（Chromium `RESULT_CODE_PROFILE_IN_USE`）。

* **默认 clone 模式**已经规避了这个问题；如果你显式加了 `--mode share`，请去掉。
* 如果仍然失败，去任务管理器把所有 `msedge.exe` / `chrome.exe` 全杀掉再试，或者改用 `--mode attach`。

### `未能进入腾讯文档桌面页，可能浏览器未登录`

clone 模式克隆出来的 profile 没有腾讯文档的有效 Cookie。请确认：

1. 你本机 Edge 的同一个 profile（默认 `Default`）确实登录过 docs.qq.com；
2. 或者改用 `--mode attach`，让脚本直接接管你已登录的浏览器。

### `找不到浏览器用户数据目录`

用 `--user-data-dir` 显式指定，例如：

```powershell
python fetch_zhuite_c6.py --user-data-dir "$env:LOCALAPPDATA\Microsoft\Edge\User Data"
```

### 没找到「骓特C6」

确认文档标题中是否真的包含「骓特C6」；若是分享到团队 / 共享空间但不在「最近 / 我的」列表里，请把文档 URL 直接发给我，我再加一个直接打开 URL 的参数。
