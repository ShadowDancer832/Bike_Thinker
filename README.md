# Bike_Thinker

抓取腾讯文档「骓特C6」并整理为列表形式的 Python 脚本。

## 工作原理

脚本使用 [Playwright](https://playwright.dev/) 的 **持久化上下文**（persistent context）
直接复用你本机 Edge / Chrome 的用户目录，从而带上已经登录好的腾讯文档 Cookie，
不需要重新登录或者手工导出 Cookie。

## 使用步骤（Windows / PowerShell）

```powershell
cd D:\CUSOR_WS\Bike_Thinker

# 1. 安装依赖
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install

# 2. 重要：先把所有 Edge（或 Chrome）窗口关掉，
#    否则 Playwright 没法复用同一个 user-data-dir。

# 3. 运行
python fetch_zhuite_c6.py
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--browser {edge,chrome}` | 选择复用哪个浏览器的登录态，默认 `edge` |
| `--profile "Profile 1"` | 浏览器 profile 目录名，默认 `Default` |
| `--user-data-dir <PATH>` | 显式指定浏览器 user-data-dir 路径 |
| `--headed` | 显示浏览器界面（排查问题时用） |
| `--out <DIR>` | 输出目录，默认当前目录 |

## 输出

* `zhuite_c6.json` — 结构化结果（在线文档为段落列表，在线表格为二维数组）。
* `zhuite_c6.csv`  — 若为在线表格则同时输出 CSV。

控制台会打印前 5 行 / 段作为预览。

## 常见问题

1. **报错：`未能进入腾讯文档桌面页，可能浏览器未登录`**
   先用同一个浏览器、同一个 profile 打开 <https://docs.qq.com> 完成登录，
   然后关闭所有浏览器窗口再跑脚本。
2. **报错：`找不到浏览器用户数据目录`**
   用 `--user-data-dir` 显式指定，例如：
   ```powershell
   python fetch_zhuite_c6.py --user-data-dir "$env:LOCALAPPDATA\Microsoft\Edge\User Data"
   ```
3. **没找到「骓特C6」**
   检查文档标题中是否真的包含「骓特C6」，或将关键字常量
   `DOC_TITLE_KEYWORD` 改成你实际的名字。
