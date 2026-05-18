"""
抓取腾讯文档「骓特C6」并整理为列表形式。

工作原理：
    使用 Playwright 启动浏览器，并 **复用本机 Edge / Chrome 的登录态**。
    有 3 种连接模式可选（`--mode`）：

      * clone（默认，推荐）
            把登录所需的 Cookies / Local State 等少量文件从本机
            Edge/Chrome 的 user-data-dir 克隆到一个临时目录，
            Playwright 用这份独立的 profile 启动浏览器。
            这样即使你本机 Edge 还在后台运行也不会冲突。

      * share
            直接把本机 Edge/Chrome 的 user-data-dir 借给 Playwright 用
            （Playwright 官方做法）。**必须**先把所有 Edge/Chrome 进程
            （包括后台 / Edge Update / 侧栏 / Widget 等）杀干净。
            否则会 `exitCode=21 (PROFILE_IN_USE)`。

      * attach
            连接到一个你自己手动启动的、开启了远程调试端口的浏览器：

                msedge.exe --remote-debugging-port=9222

            然后运行：

                python fetch_zhuite_c6.py --mode attach --cdp-port 9222

使用方法（Windows，PowerShell）：

    cd D:\\CUSOR_WS\\Bike_Thinker

    python -m venv .venv
    .venv\\Scripts\\Activate.ps1
    pip install -r requirements.txt
    python -m playwright install

    # 默认：clone 模式，最省事
    python fetch_zhuite_c6.py

    # Chrome 用户：
    python fetch_zhuite_c6.py --browser chrome
    # 非默认 profile（比如 "Profile 1"）：
    python fetch_zhuite_c6.py --profile "Profile 1"
    # 看着浏览器跑：
    python fetch_zhuite_c6.py --headed

输出文件：
    zhuite_c6.json   结构化结果（list[dict] 或 list[list]）
    zhuite_c6.csv    若为表格则同时生成 CSV
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import re
import shutil
import sys
import tempfile
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

# 克隆 profile 时需要复制的文件（保留登录 Cookie 与解密密钥即可，量很小）
TOP_LEVEL_FILES = ["Local State"]
PROFILE_FILES = [
    "Cookies",
    "Cookies-journal",
    "Cookies-wal",
    "Cookies-shm",
    "Login Data",
    "Login Data-journal",
    "Preferences",
    "Secure Preferences",
    "Web Data",
    "Web Data-journal",
    "Network/Cookies",
    "Network/Cookies-journal",
    "Network/Network Persistent State",
    "Network/Trust Tokens",
]


def default_user_data_dir(browser: str) -> Path:
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


def _win_copy_shared(src: Path, dst: Path) -> None:
    """在 Windows 上用 CreateFileW + 全部 FILE_SHARE_* 旗标去读源文件，
    然后写到 dst。比 shutil.copy2 更能绕开 Edge 持有的非独占锁。
    若 Edge 用 FILE_SHARE_NONE 锁住文件，此方法仍会失败。"""
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004  # read|write|delete
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    CreateFileW = k32.CreateFileW
    CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    CreateFileW.restype = wintypes.HANDLE
    ReadFile = k32.ReadFile
    ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    ReadFile.restype = wintypes.BOOL
    CloseHandle = k32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    handle = CreateFileW(
        str(src), GENERIC_READ, FILE_SHARE_ALL, None,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        raise OSError(err, f"CreateFileW failed (WinErr {err}) for {src}")
    try:
        BUFSIZE = 1024 * 1024
        buf = (ctypes.c_ubyte * BUFSIZE)()
        with open(dst, "wb") as out:
            while True:
                got = wintypes.DWORD(0)
                ok = ReadFile(handle, buf, BUFSIZE, ctypes.byref(got), None)
                if not ok:
                    err = ctypes.get_last_error()
                    raise OSError(err, f"ReadFile failed (WinErr {err}) for {src}")
                if got.value == 0:
                    break
                out.write(bytes(buf[: got.value]))
    finally:
        CloseHandle(handle)


def _copy_file(src: Path, dst: Path) -> None:
    """先尝试 shutil.copy2；Windows 上若被独占锁挡了，则降级到 CreateFileW 共享读取。"""
    try:
        shutil.copy2(src, dst)
        return
    except PermissionError:
        if not sys.platform.startswith("win"):
            raise
    _win_copy_shared(src, dst)


def clone_profile(src: Path, profile: str) -> Path:
    """把 src/<profile> 下的登录相关文件复制到一个临时 user-data-dir 中并返回。
    复制目标固定在 %TEMP%\\bike_thinker_browser_profile，多次运行可复用。"""
    dst = Path(tempfile.gettempdir()) / "bike_thinker_browser_profile"
    dst.mkdir(parents=True, exist_ok=True)

    # 移除可能残留的 SingletonLock / SingletonCookie / SingletonSocket
    for stale in dst.glob("Singleton*"):
        try:
            stale.unlink()
        except Exception:
            pass

    if not src.exists():
        raise RuntimeError(
            f"找不到源浏览器用户数据目录：{src}\n"
            f"请用 --user-data-dir 显式指定，或确认 --browser/--profile 参数正确。"
        )

    src_profile = src / profile
    if not src_profile.exists():
        raise RuntimeError(
            f"找不到 profile 目录：{src_profile}\n"
            f"请用 --profile 指定正确名称（Edge 默认是 Default，第二个 profile 通常是 'Profile 1'）。"
        )

    failed_critical: list[Path] = []

    # 顶层文件
    for f in TOP_LEVEL_FILES:
        s = src / f
        if s.exists():
            try:
                _copy_file(s, dst / f)
            except Exception as e:
                print(f"[WARN] 复制 {s} 失败：{e}", file=sys.stderr)
                if f == "Local State":
                    failed_critical.append(s)

    # profile 内文件
    dst_profile = dst / profile
    (dst_profile / "Network").mkdir(parents=True, exist_ok=True)
    copied = 0
    cookies_copied = False
    for f in PROFILE_FILES:
        s = src_profile / f
        if s.exists():
            d = dst_profile / f
            d.parent.mkdir(parents=True, exist_ok=True)
            try:
                _copy_file(s, d)
                copied += 1
                if f in ("Cookies", "Network/Cookies"):
                    cookies_copied = True
            except Exception as e:
                print(f"[WARN] 复制 {s} 失败：{e}", file=sys.stderr)
                if f in ("Cookies", "Network/Cookies"):
                    failed_critical.append(s)

    if copied == 0:
        raise RuntimeError(
            f"源 profile {src_profile} 里没有发现可复制的登录文件。"
            f"请确认你确实用过该浏览器登录腾讯文档。"
        )

    if not cookies_copied:
        msg = (
            "关键的 Cookies 文件没能复制下来——你本机 Edge 正在用它（独占锁），\n"
            "Windows 不允许另一个进程读取。请改用下面任一方式：\n"
            "  (A) 推荐：用 attach 模式，不需要复制任何文件\n"
            "        1) & \"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe\" "
            "--remote-debugging-port=9222\n"
            "        2) python fetch_zhuite_c6.py --mode attach --cdp-port 9222\n"
            "  (B) 任务管理器结束所有 msedge.exe 进程，然后重跑本脚本。"
        )
        raise RuntimeError(msg)

    print(f"[OK] 已克隆 profile 到 {dst}（复制了 {copied} 个文件）")
    return dst


def launch_context(
    p, browser: str, user_data_dir: Path, headed: bool, profile: str
) -> BrowserContext:
    """启动一个 Playwright 持久化上下文。"""
    channel = "msedge" if browser == "edge" else "chrome"
    args = [
        f"--profile-directory={profile}",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    return p.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        channel=channel,
        headless=not headed,
        args=args,
        viewport={"width": 1440, "height": 900},
    )


def connect_cdp(p, port: int) -> BrowserContext:
    """连接到用户手动启动的、开启远程调试的浏览器。"""
    endpoint = f"http://127.0.0.1:{port}"
    browser = p.chromium.connect_over_cdp(endpoint)
    if not browser.contexts:
        raise RuntimeError(
            f"通过 {endpoint} 连接到浏览器，但没有任何上下文。请确认浏览器至少打开了一个窗口。"
        )
    return browser.contexts[0]


def find_document_url(context: BrowserContext, keyword: str) -> str:
    """在腾讯文档桌面页搜索文档，返回第一个匹配项的 URL。"""
    page = context.new_page()
    page.goto(DESKTOP_URL, wait_until="domcontentloaded", timeout=60_000)

    try:
        page.wait_for_url(re.compile(r"docs\.qq\.com/desktop"), timeout=15_000)
    except PWTimeoutError:
        raise RuntimeError(
            "未能进入腾讯文档桌面页，可能克隆的 profile 没有带上有效登录。\n"
            "请确认本机浏览器是用同一个 profile 登录 https://docs.qq.com 的，"
            "或改用 --mode attach 直接连接已登录的浏览器。"
        )

    page.wait_for_load_state("networkidle", timeout=30_000)

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

    candidates = page.locator(f'a:has-text("{keyword}")')
    count = candidates.count()
    if count == 0:
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
    """抽取在线表格内容。"""
    page.wait_for_load_state("networkidle", timeout=60_000)
    page.wait_for_timeout(2500)

    page.wait_for_selector(
        '#alloy-simple-spreadsheet, .alloy-simple-spreadsheet, '
        '[class*="sheet"] canvas',
        timeout=30_000,
    )

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

    try:
        page.wait_for_selector(
            ".doc, .editor-body, [contenteditable='true']", timeout=30_000
        )
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


def resolve_user_data_dir(args: argparse.Namespace) -> Path:
    """根据 --mode / --user-data-dir 决定真正给 Playwright 用的目录。"""
    src = (
        Path(args.user_data_dir)
        if args.user_data_dir
        else default_user_data_dir(args.browser)
    )

    if args.mode == "share":
        if not src.exists():
            raise RuntimeError(f"找不到浏览器用户数据目录：{src}")
        print(
            "[WARN] share 模式要求所有 Edge/Chrome 进程已退出，否则会 exitCode=21。\n"
            "       建议改用默认的 clone 模式：去掉 --mode share。"
        )
        return src

    # 默认 clone
    return clone_profile(src, args.profile)


def fetch(args: argparse.Namespace) -> dict[str, Any]:
    with sync_playwright() as p:
        if args.mode == "attach":
            ctx = connect_cdp(p, args.cdp_port)
        else:
            user_data_dir = resolve_user_data_dir(args)
            ctx = launch_context(
                p, args.browser, user_data_dir, args.headed, args.profile
            )

        try:
            ctx.grant_permissions(
                ["clipboard-read", "clipboard-write"], origin="https://docs.qq.com"
            )
        except Exception:
            pass

        try:
            url = find_document_url(ctx, DOC_TITLE_KEYWORD)
            print(f"[OK] 找到文档：{url}")

            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
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
            try:
                if args.mode == "attach":
                    # attach 模式不要关浏览器，留给用户
                    pass
                else:
                    ctx.close()
            except Exception:
                pass


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
    parser = argparse.ArgumentParser(
        description="抓取腾讯文档「骓特C6」并整理为列表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "推荐用法：直接 `python fetch_zhuite_c6.py` 即可（默认 clone 模式，"
            "不会和你正在运行的 Edge 冲突）。"
        ),
    )
    parser.add_argument(
        "--browser", choices=["edge", "chrome"], default="edge",
        help="使用哪个本机浏览器复用登录态（默认 edge）",
    )
    parser.add_argument(
        "--profile", default="Default",
        help='浏览器 profile 目录名，默认 "Default"，常见还有 "Profile 1"',
    )
    parser.add_argument(
        "--user-data-dir", default=None,
        help="显式指定浏览器 user-data-dir 路径，覆盖自动探测",
    )
    parser.add_argument(
        "--mode", choices=["clone", "share", "attach"], default="clone",
        help="登录态复用方式：clone 克隆 profile（默认）/ share 直接借用（需关闭浏览器）"
             " / attach 连接到带 --remote-debugging-port 的现成浏览器",
    )
    parser.add_argument(
        "--cdp-port", type=int, default=9222,
        help="attach 模式下连接的远程调试端口（默认 9222）",
    )
    parser.add_argument("--headed", action="store_true",
                        help="显示浏览器界面（便于排查问题）")
    parser.add_argument("--out", default=".", help="输出目录，默认当前目录")
    args = parser.parse_args()

    try:
        result = fetch(args)
    except Exception as e:
        msg = str(e)
        print(f"[ERR] {msg}", file=sys.stderr)
        if "has been closed" in msg or "exitCode=21" in msg or "Profile" in msg:
            print(
                "\n排错建议：\n"
                "  * 你大概率遇到了 Edge/Chrome 的 user-data-dir 被占用（exitCode=21）。\n"
                "  * 默认 clone 模式应该已经避免了这个问题；如果你显式加了 --mode share，请去掉。\n"
                "  * 仍然失败时，尝试：\n"
                "      1) 任务管理器里彻底结束所有 msedge.exe 进程后重试；\n"
                "      2) 或者手动启动一个调试 Edge：\n"
                "             \"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe\" "
                "--remote-debugging-port=9222\n"
                "         在该 Edge 里登录腾讯文档，然后另开终端运行：\n"
                "             python fetch_zhuite_c6.py --mode attach --cdp-port 9222\n",
                file=sys.stderr,
            )
        return 1

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
