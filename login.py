#!/usr/bin/env python3
"""doubao-mcp 独立扫码登录 —— 抓 sessionid 写入 ~/.doubao-mcp/accounts.json

本脚本让 doubao-web-mcp 完全脱离 doubao2api：登录凭证由自己抓取。

用法
----
  # 无 GUI 环境（沙箱/后台）：截图二维码 → 手机豆包 App / 微信扫码
  ~/doubao-mcp/.venv/bin/python ~/doubao-mcp/login.py

  # 在自己终端跑，用可见浏览器窗口扫码
  ~/doubao-mcp/.venv/bin/python ~/doubao-mcp/login.py --headed

  # 指定账号名（默认按时间戳命名）
  ~/doubao-mcp/.venv/bin/python ~/doubao-mcp/login.py --name 我的主号

  # 账号已登录过、只想续期
  ~/doubao-mcp/.venv/bin/python ~/doubao-mcp/login.py --name 我的主号 --timeout 60
"""
import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from playwright.async_api import async_playwright

POOL = Path(os.environ.get("DOUBAO_ACCOUNTS_FILE", "~/.doubao-mcp/accounts.json")).expanduser()
LOGIN_URL = "https://www.doubao.com/chat/"

# 伪装成正常桌面 Chrome —— headless 默认 UA 带 "HeadlessChrome"，容易被风控识别
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")

# 只保留豆包相关 cookie，避免把无关追踪 cookie 写进凭证文件
KEEP = {
    "sessionid", "ttwid", "s_v_web_id", "i18next", "passport_csrf_token",
    "passport_csrf_token_default", "flow_user_country", "flow_cur_user_sec_id",
    "dbx-web-theme", "conversation_list_v2_group_mode", "msToken",
}


def save_account(name: str, cookies: dict) -> str:
    """写入/更新账号池文件（同名覆盖），权限 600。"""
    POOL.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(POOL.parent, 0o700)

    data = {"version": 1, "active": None, "accounts": []}
    if POOL.exists():
        try:
            data = json.loads(POOL.read_text())
        except Exception:
            pass

    accounts = [a for a in data.get("accounts", []) if a.get("name") != name]
    accounts.append({"name": name, "cookies": cookies})
    data["accounts"] = accounts
    data.setdefault("active", name)

    POOL.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.chmod(POOL, 0o600)
    return str(POOL)


# 关掉页面加载约 10s 后自动弹出的「下载豆包电脑版」推广窗。
# 它总盖在登录窗前面（Playwright 的元素截图会把覆盖物一起拍进去）。
# 做法：从所有含该文案的 div 各自向上找到「定位 + 尺寸小于视口」的弹窗本体并隐藏；
# 条件必须收紧到带定位且小于视口，否则会误伤整个 app 根节点（曾导致整页白屏）。
HIDE_PROMO = """() => {
    const vw = innerWidth, vh = innerHeight;
    const hits = Array.from(document.querySelectorAll('div'))
        .filter(e => (e.innerText || '').includes('下载豆包电脑版'));
    let n = 0;
    for (const t of hits) {
        let e = t;
        while (e && e !== document.body) {
            const cs = getComputedStyle(e), r = e.getBoundingClientRect();
            if (/fixed|absolute/.test(cs.position) && r.width < vw * 0.95 && r.height < vh * 0.95) {
                e.style.setProperty('display', 'none', 'important');
                n++;
                break;
            }
            e = e.parentElement;
        }
    }
    return n;
}"""

# 定位登录弹窗本体，用于只截登录窗（放大二维码，方便手机扫）
FIND_MODAL = """() => {
    const vw = innerWidth, vh = innerHeight;
    const t = Array.from(document.querySelectorAll('div'))
        .find(e => (e.innerText || '').includes('使用豆包或'));
    if (!t) return null;
    let n = t;
    while (n && n !== document.body) {
        const cs = getComputedStyle(n), r = n.getBoundingClientRect();
        if (/fixed|absolute/.test(cs.position) && r.width < vw * 0.95 && r.height < vh * 0.95) return n;
        n = n.parentElement;
    }
    return null;
}"""


async def shoot_qr(page, out_path):
    """截登录窗（优先）；找不到就整页截。每轮先清一次推广窗。"""
    try:
        await page.evaluate(HIDE_PROMO)
    except Exception:
        pass
    await page.wait_for_timeout(400)
    try:
        handle = await page.evaluate_handle(FIND_MODAL)
        el = handle.as_element()
        if el:
            await el.screenshot(path=out_path)
            return out_path
    except Exception:
        pass
    await page.screenshot(path=out_path)
    return out_path


async def main():
    ap = argparse.ArgumentParser(description="豆包扫码登录（doubao-web-mcp 独立凭证获取）")
    ap.add_argument("--headed", action="store_true", help="可见浏览器（需在你自己终端跑）")
    ap.add_argument("--name", default="", help="账号名，默认按时间戳命名")
    ap.add_argument("--timeout", type=int, default=300, help="等待扫码的超时秒数")
    ap.add_argument("--qr-out", default="/tmp/doubao_login_qr.png", help="二维码截图路径")
    args = ap.parse_args()

    name = args.name or f"账号-{time.strftime('%m%d-%H%M')}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        ctx = await browser.new_context(
            user_agent=UA, locale="zh-CN", timezone_id="Asia/Shanghai",
            viewport={"width": 1280, "height": 900},
            device_scale_factor=2,      # 2 倍图，截图里的二维码手机更好扫
        )
        page = await ctx.new_page()

        print(f"→ 打开豆包登录页：{LOGIN_URL}")
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"❌ 打开页面失败：{str(e)[:200]}")
            await browser.close()
            return 1
        await page.wait_for_timeout(3500)   # 页面骨架（别等太久，推广窗约 10s 后弹）

        # 已登录过（上下文里有 sessionid）就直接存
        cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
        if cookies.get("sessionid"):
            path = save_account(name, {k: v for k, v in cookies.items() if k in KEEP})
            print(f"✅ 检测到已登录，凭证已保存：{path}（账号：{name}）")
            await browser.close()
            return 0

        # 未登录：点右上角「登录」
        # 用 JS 找「可见的、文本恰好是 登录 的按钮」——CSS 类名带构建哈希会变，不可靠
        clicked = await page.evaluate("""() => {
            const hit = Array.from(document.querySelectorAll('button'))
                .find(b => (b.innerText || '').trim() === '登录' && b.offsetParent !== null);
            if (hit) { hit.click(); return true; }
            return false;
        }""")
        print("→ 已点开登录弹窗" if clicked else "⚠️ 没找到登录按钮（页面结构可能变了）")
        await page.wait_for_timeout(2500)

        # 勾上「已阅读并同意用户协议」，否则扫码后可能不落地登录态
        await page.evaluate("""() => {
            const cb = Array.from(document.querySelectorAll('input[type=checkbox], [role=checkbox]'))
                .find(e => e.offsetParent !== null && !e.checked);
            if (cb) { cb.click(); return true; }
            return false;
        }""")

        if args.headed:
            print("📱 请在弹出的浏览器窗口里扫码/登录，完成后本脚本会自动保存凭证…")
        else:
            await shoot_qr(page, args.qr_out)
            print(f"📱 二维码已截图：{args.qr_out}")
            print("   用豆包 App 或飞书 App 扫这个图（沙箱无 GUI，headless 下只能看截图）")

        deadline = time.time() + args.timeout
        tick = 0
        while time.time() < deadline:
            await page.wait_for_timeout(3000)
            tick += 1
            cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
            if cookies.get("sessionid"):
                kept = {k: v for k, v in cookies.items() if k in KEEP and v}
                path = save_account(name, kept)
                print(f"\n✅ 登录成功（等待 {tick * 3}s）")
                print(f"   账号名：{name}")
                print(f"   cookie：{', '.join(kept.keys())}")
                print(f"   凭证文件：{path}")
                print("\n现在可以直接调 doubao_ping / doubao_chat / doubao_image / doubao_video 了。")
                await browser.close()
                return 0
            # 二维码有有效期，过期后重截一张；顺带再清一次推广窗
            if tick % 5 == 0 and not args.headed:
                await shoot_qr(page, args.qr_out)
                print(f"   （二维码可能已刷新，最新截图：{args.qr_out}）")

        print(f"\n⏱ 超时（{args.timeout}s）未检测到登录。二维码截图：{args.qr_out}")
        print("   若截图里不是二维码，说明豆包识别到了自动化，改用：")
        print("   ~/doubao-mcp/.venv/bin/python ~/doubao-mcp/login.py --headed")
        await browser.close()
        return 2


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(main()))
