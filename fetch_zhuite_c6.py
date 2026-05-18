"""
抓取腾讯文档「骓特C6」并整理为列表形式。

工作原理：
    使用 Playwright 启动一个 **持久化上下文**（persistent context），
    直接复用本机浏览器（Edge/Chrome）的用户目录，从而带上你已经登录好的
    腾讯文档 Cookie。脚本会：

        1. 打开 https://docs.qq.com/desktop  寻找标题包含「骓特C6」的文档。
        2. 进入该文档，根据是「在线表格」还是「在线文档」分别抽取内容。
        3. 将结果整理为 Python list，并同时落盘成 JSON / CSV，方便后续处理。

使用方法（Windows，PowerShell）：

    # 0. 在 D:\\CUSOR_WS\\Bike_Thinker 下放置本脚本
    cd D:\\CUSOR_WS\\Bike_Thinker

    # 1. 安装依赖
    python -m venv .venv
    .venv\\Scripts\\Activate.ps1
    pip install -r requirements.txt
    python -m playwright install

    # 2. （重要）先关闭所有 Edge / Chrome 窗口，
    #    否则 Playwright 无法复用同一个 user-data-dir。
    # 3. 运行
    python fetch_zhuite_c6.py

    #    如果想用 Chrome：
    python fetch_zhuite_c6.py --browser chrome
    #    如果你的浏览器使用了非默认 profile：
    python fetch_zhuite_c6.py --profile "Profile 1"
    #    想看着浏览器跑：
    python fetch_zhuite_c6.py --headed

输出文件：
    zhuite_c6.json   结构化结果（list[dict] 或 list[list]）
    zhuite_c6.csv    若为表格则同时生成 CSV
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PWTimeoutError,
    sync_playwright,
)


DOC_TITLE_KEYWORD = "骓特C6"
DESKTOP_URL = "https://docs.qq.com/desktop/"


def default_user_data_dir(browser: str, profile: str) -> Path:
    """返回当前操作系统下浏览器默认的 user-data-dir。"""
    home = Path.home()
    if sys.platform.startswith("win"):
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        if browser == "edge":
            return local / "Microsoft" / "Edge" / "User Data"
        return local / "Google" / "Chrome" / "User Data"
    if sys.platform == "darwin":
        if browser == "edge":
            return home / "Library" / "Application Support" / "Microsoft Edge"
        return home / "Library" / "Application Support" / "Google" / "Chrome"
    # Linux
    if browser == "edge":
        return home / ".config" / "microsoft-edge"
    return home / ".config" / "google-chrome"


def launch_context(
    p, browser: str, user_data_dir: Path, headed: bool, profile: str
) -> BrowserContext:
    """启动一个复用本机登录态的浏览器上下文。"""
    channel = "msedge" if browser == "edge" else "chrome"
    args = [
        f"--profile-directory={profile}",
        "--disable-blink-features=AutomationControlled",
    ]
    return p.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        channel=channel,
        headless=not headed,
        args=args,
        viewport={"width": 1440, "height": 900},
    )


def find_document_url(context: BrowserContext, keyword: str) -> str:
    """在腾讯文档桌面页搜索文档，返回第一个匹配项的 URL。"""
    page = context.new_page()
    page.goto(DESKTOP_URL, wait_until="domcontentloaded")

    # 若未登录则给出明确报错
    try:
        page.wait_for_url(re.compile(r"docs\.qq\.com/desktop"), timeout=15_000)
    except PWTimeoutError:
        raise RuntimeError(
            "未能进入腾讯文档桌面页，可能浏览器未登录。请先在本机浏览器登录 "
            "https://docs.qq.com，并关闭所有该浏览器窗口后重试。"
        )

    page.wait_for_load_state("networkidle", timeout=30_000)

    # 优先用顶部搜索框筛选
    search_selectors = [
        'input[placeholder*="搜索"]',
        'input[type="search"]',
        '[class*="search"] input',
    ]
    used_search = False
    for sel in search_selectors:
        try:
            box = page.locator(sel).first
            if box.count() and box.is_visible():
                box.click()
                box.fill(keyword)
                page.keyboard.press("Enter")
                used_search = True
                break
        except Exception:
            continue

    page.wait_for_timeout(2500)

    # 查找标题包含关键字的链接
    candidates = page.locator(f'a:has-text("{keyword}")')
    count = candidates.count()
    if count == 0:
        # 兜底：滚动加载列表后再找一次
        for _ in range(5):
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(800)
        candidates = page.locator(f'a:has-text("{keyword}")')
        count = candidates.count()

    if count == 0:
        raise RuntimeError(
            f"在腾讯文档桌面页没找到包含「{keyword}」的文档。"
            f"（搜索框{'已使用' if used_search else '未找到'}）"
        )

    # 取第一个 href 包含 docs.qq.com 的
    for i in range(count):
        href = candidates.nth(i).get_attribute("href") or ""
        if "docs.qq.com" in href or href.startswith("/"):
            if href.startswith("/"):
                href = "https://docs.qq.com" + href
            page.close()
            return href

    page.close()
    raise RuntimeError("找到匹配项但无法获取 URL。")


def extract_sheet(page: Page) -> list[list[str]]:
    """抽取在线表格内容。腾讯文档表格的可见单元格在 canvas 中渲染，
    但顶部 DOM 里仍能拿到完整数据；我们通过 window 全局变量 + 选择全部复制
    两条路径中较稳的一条——直接读取单元格 DOM 的 textContent。"""
    page.wait_for_load_state("networkidle", timeout=60_000)
    page.wait_for_timeout(2500)

    # 等表格容器渲染
    page.wait_for_selector(
        '#alloy-simple-spreadsheet, .alloy-simple-spreadsheet, '
        '[class*="sheet"] canvas',
        timeout=30_000,
    )

    # 通过键盘 Ctrl+A、Ctrl+C 然后从剪贴板拿数据
    page.keyboard.press("Control+A")
    page.wait_for_timeout(300)
    page.keyboard.press("Control+C")
    page.wait_for_timeout(800)

    text = ""
    try:
        text = page.evaluate("navigator.clipboard.readText()")
    except Exception:
        text = ""

    if not text:
        # 兜底：抓取所有 textarea / 单元格 DOM
        text = page.evaluate(
            """
            () => {
                const cells = document.querySelectorAll('[role="gridcell"], .cell');
                return Array.from(cells).map(c => c.innerText).join('\\n');
            }
            """
        )

    rows: list[list[str]] = []
    for line in text.splitlines():
        if line == "":
            continue
        rows.append(line.split("\t"))
    return rows


def extract_doc(page: Page) -> list[str]:
    """抽取在线文档（word 类）内容，按段落整理成 list[str]。"""
    page.wait_for_load_state("networkidle", timeout=60_000)
    page.wait_for_timeout(2500)

    # 腾讯文档的正文区域
    try:
        page.wait_for_selector(".doc, .editor-body, [contenteditable='true']", timeout=30_000)
    except PWTimeoutError:
        pass

    paragraphs: list[str] = page.evaluate(
        """
        () => {
            const root = document.querySelector('.doc, .editor-body, [contenteditable="true"]')
                       || document.body;
            const nodes = root.querySelectorAll(
                'p, li, h1, h2, h3, h4, h5, h6, tr, .ql-block'
            );
            const out = [];
            nodes.forEach(n => {
                const t = (n.innerText || '').trim();
                if (t) out.push(t);
            });
            return out;
        }
        """
    )
    return [p for p in paragraphs if p]


def fetch(args: argparse.Namespace) -> dict[str, Any]:
    user_data_dir = Path(args.user_data_dir) if args.user_data_dir else default_user_data_dir(args.browser, args.profile)
    if not user_data_dir.exists():
        raise RuntimeError(
            f"找不到浏览器用户数据目录：{user_data_dir}\n"
            f"可用 --user-data-dir 显式指定。"
        )

    with sync_playwright() as p:
        ctx = launch_context(p, args.browser, user_data_dir, args.headed, args.profile)
        # 给剪贴板权限，便于读取表格内容
        try:
            ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://docs.qq.com")
        except Exception:
            pass

        try:
            url = find_document_url(ctx, DOC_TITLE_KEYWORD)
            print(f"[OK] 找到文档：{url}")

            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=60_000)
            time.sleep(2)

            is_sheet = "/sheet/" in page.url or page.locator("canvas").count() > 0
            if is_sheet:
                rows = extract_sheet(page)
                result: dict[str, Any] = {
                    "title": DOC_TITLE_KEYWORD,
                    "url": page.url,
                    "type": "sheet",
                    "rows": rows,
                }
            else:
                paragraphs = extract_doc(page)
                result = {
                    "title": DOC_TITLE_KEYWORD,
                    "url": page.url,
                    "type": "doc",
                    "paragraphs": paragraphs,
                }

            return result
        finally:
            ctx.close()


def save(result: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "zhuite_c6.json"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[OK] 已写入 {json_path}")

    if result["type"] == "sheet":
        csv_path = out_dir / "zhuite_c6.csv"
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            for row in result["rows"]:
                w.writerow(row)
        print(f"[OK] 已写入 {csv_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取腾讯文档「骓特C6」并整理为列表")
    parser.add_argument("--browser", choices=["edge", "chrome"], default="edge",
                        help="使用哪个本机浏览器复用登录态（默认 edge）")
    parser.add_argument("--profile", default="Default",
                        help='浏览器 profile 目录名，默认 "Default"，常见还有 "Profile 1"')
    parser.add_argument("--user-data-dir", default=None,
                        help="显式指定浏览器 user-data-dir 路径，覆盖自动探测")
    parser.add_argument("--headed", action="store_true",
                        help="显示浏览器界面（便于排查问题）")
    parser.add_argument("--out", default=".", help="输出目录，默认当前目录")
    args = parser.parse_args()

    try:
        result = fetch(args)
    except Exception as e:
        print(f"[ERR] {e}", file=sys.stderr)
        return 1

    # 控制台打印精简列表预览
    if result["type"] == "sheet":
        rows = result["rows"]
        print(f"[INFO] 表格共 {len(rows)} 行，预览前 5 行：")
        for r in rows[:5]:
            print("  -", r)
    else:
        paras = result["paragraphs"]
        print(f"[INFO] 文档共 {len(paras)} 段，预览前 5 段：")
        for s in paras[:5]:
            print("  -", s)

    save(result, Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
