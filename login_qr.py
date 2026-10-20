#!/usr/bin/env python3
"""doubao-mcp 扫码登录 v2（login_qr.py）—— 修复版

修复内容（相比 login.py）：
1. 「下载豆包电脑版」推广窗在 shadow DOM 里，HIDE_PROMO（按 innerText 找 div）藏不掉它，
   会盖住登录弹窗导致截错图。改为：shadow DOM 穿透找 aria-label 含「关闭」的按钮点掉。
2. 登录弹窗默认展示「手机号登录 / 飞书一键登录」，二维码在右侧但需要点底部
   「扫码登录」tab 才渲染。改为：点登录弹窗后自动切到扫码 tab。
3. 推广窗关闭会把登录弹窗一起关掉 → 关推广窗后重新点一次「登录」再切 tab。
4. 协议勾选框在 shadow DOM 里，evaluate 版的 querySelectorAll 找不到它 → 勾了等于没勾，
   扫码后登录态不落地。改为 Playwright 原生 locator（自动穿透 open shadow DOM + 真实鼠标事件）。
5. 登录轮询间隔 3s → 1.5s（手机端点完确认，登录态落地更快被捕获）。
6. 新增「确认登录」按钮自动点击（部分情况下网页端会出现二次确认按钮）。

用法：
  # 无 GUI（沙箱）：截二维码图 → 手机豆包/飞书 App 扫
  ~/doubao-mcp/.venv/bin/python ~/doubao-mcp/login_qr.py

  ~/doubao-mcp/.venv/bin/python ~/doubao-mcp/login_qr.py --name 我的新号 --timeout 300
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).parent))
from login import KEEP, UA, save_account  # 复用凭证落盘逻辑（600 权限、同名覆盖）

LOGIN_URL = "https://www.doubao.com/chat/"

CLICK_LOGIN_JS = """() => {
    const hit = Array.from(document.querySelectorAll('button'))
        .find(b => (b.innerText || '').trim() === '登录' && b.offsetParent !== null);
    if (hit) { hit.click(); return true; }
    return false;
}"""

CHECK_AGREEMENT_JS = """() => {
    const cb = Array.from(document.querySelectorAll('input[type=checkbox], [role=checkbox]'))
        .find(e => e.offsetParent !== null && !e.checked);
    if (cb) { cb.click(); return true; }
    return false;
}"""

# 推广窗/登录弹窗整体在 shadow DOM 里，必须穿透查找关闭按钮
SHADOW_CLOSE_JS = """() => {
    const results = [];
    function walk(root) {
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) walk(el.shadowRoot);
            const label = (el.getAttribute && (el.getAttribute('aria-label') || '')) || '';
            const isBtn = el.tagName === 'BUTTON' || (el.getAttribute && el.getAttribute('role') === 'button');
            if (isBtn && /关闭|close|×|✕/i.test(label)) {
                const r = el.getBoundingClientRect();
                if (r.width > 0) results.push({x: Math.round(r.left + r.width/2),
                                               y: Math.round(r.top + r.height/2), label});
            }
        }
    }
    walk(document);
    return results;
}"""

# 「扫码登录」是底部 tab（span），用 dispatchEvent 点，不依赖元素是否在最上层
CLICK_SCAN_TAB_JS = """() => {
    const els = Array.from(document.querySelectorAll('div, span, li, p, a'))
        .filter(e => (e.innerText || '').trim() === '扫码登录'
                     && e.offsetParent !== null && e.children.length === 0);
    if (!els.length) return 'not-found';
    const el = els[0];
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    const target = document.elementFromPoint(x, y) || el;
    for (const ev of ['mousedown', 'mouseup', 'click']) {
        target.dispatchEvent(new MouseEvent(ev, {bubbles: true, clientX: x, clientY: y}));
    }
    return 'ok';
}"""

# 扫码后网页端偶尔出现二次确认按钮
CLICK_CONFIRM_JS = """() => {
    const hit = Array.from(document.querySelectorAll('button, div[role=button], span'))
        .find(b => {
            const t = (b.innerText || '').trim();
            return /^(确认登录|确认|完成登录|同意并继续)$/.test(t) && b.offsetParent !== null;
        });
    if (hit) { hit.click(); return (hit.innerText || '').trim(); }
    return null;
}"""

FIND_MODAL_JS = """() => {
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


async def check_agreement(page) -> bool:
    """勾「已阅读并同意」协议。

    关键：协议框在 shadow DOM 内，evaluate 版 querySelectorAll 拿不到真实控件；
    Playwright 的 locator 会自动穿透 open shadow root，且 click 走真实鼠标事件。
    """
    for sel in ("[role=checkbox]", "input[type=checkbox]"):
        try:
            loc = page.locator(sel)
            n = await loc.count()
        except Exception:
            continue
        for i in range(min(n, 4)):
            item = loc.nth(i)
            try:
                if not await item.is_visible():
                    continue
                if await item.is_checked():
                    return True
                await item.click(force=True, timeout=5000)
                await page.wait_for_timeout(400)
                if await item.is_checked():
                    return True
            except Exception:
                continue
    # 兜底：老逻辑
    try:
        if await page.evaluate(CHECK_AGREEMENT_JS):
            return True
    except Exception:
        pass
    return False


async def close_promo(page) -> bool:
    """关掉推广窗（它会连带关掉登录弹窗，所以之后要重点登录）。"""
    btns = await page.evaluate(SHADOW_CLOSE_JS)
    if btns:
        await page.mouse.click(btns[0]["x"], btns[0]["y"])
        await page.wait_for_timeout(1200)
        return True
    return False


async def open_login_and_scan_tab(page) -> bool:
    """点登录 → 勾协议 → 切扫码 tab。返回是否成功切到扫码 tab。"""
    await page.evaluate(CLICK_LOGIN_JS)
    await page.wait_for_timeout(2500)
    agreed = await check_agreement(page)
    await page.wait_for_timeout(800)
    r = await page.evaluate(CLICK_SCAN_TAB_JS)
    await page.wait_for_timeout(2500)
    print(f"   协议勾选: {'✅' if agreed else '⚠️ 未确认已勾选'}")
    return r == "ok"


async def shoot_qr(page, out_path) -> bool:
    """截登录弹窗（含二维码）。返回是否截到。"""
    handle = await page.evaluate_handle(FIND_MODAL_JS)
    el = handle.as_element()
    if el:
        await el.screenshot(path=out_path)
        return True
    await page.screenshot(path=out_path)
    return False


async def main():
    ap = argparse.ArgumentParser(description="豆包扫码登录 v2（修推广窗遮挡 + 自动切扫码 tab + 协议勾选）")
    ap.add_argument("--name", default="", help="账号名，默认按时间戳命名")
    ap.add_argument("--timeout", type=int, default=300, help="等待扫码的超时秒数")
    ap.add_argument("--qr-out", default="/tmp/doubao_login_qr.png", help="二维码截图路径")
    args = ap.parse_args()

    name = args.name or f"账号-{time.strftime('%m%d-%H%M')}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=UA, locale="zh-CN", timezone_id="Asia/Shanghai",
            viewport={"width": 1280, "height": 900},
            device_scale_factor=2,  # 2 倍图，手机更好扫
        )
        page = await ctx.new_page()

        print(f"→ 打开豆包登录页：{LOGIN_URL}")
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"❌ 打开页面失败：{str(e)[:200]}")
            await browser.close()
            return 1
        await page.wait_for_timeout(4000)

        cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
        if cookies.get("sessionid"):
            path = save_account(name, {k: v for k, v in cookies.items() if k in KEEP and v})
            print(f"✅ 检测到已登录，凭证已保存：{path}（账号：{name}）")
            await browser.close()
            return 0

        # 第一轮：点登录 → 等推广窗弹出 → 关掉（会连带关掉登录弹窗）
        print("→ 点开登录弹窗，等待推广窗…")
        await page.evaluate(CLICK_LOGIN_JS)
        await page.wait_for_timeout(2500)
        await check_agreement(page)
        await page.wait_for_timeout(5000)
        if await close_promo(page):
            print("→ 推广窗已关闭（登录弹窗被连带关闭，重新打开）")

        # 第二轮：重新点登录 → 勾协议 → 切扫码 tab（推广窗一般只弹一次）
        ok = await open_login_and_scan_tab(page)
        if not ok:
            print("⚠️ 没切到扫码 tab（页面结构可能变了），尝试直接截图")
        else:
            print("→ 已切到「扫码登录」tab")

        if await shoot_qr(page, args.qr_out):
            print(f"📱 二维码已截图：{args.qr_out}")
        print("   用豆包 App 或飞书 App 扫这个图")

        deadline = time.time() + args.timeout
        tick = 0
        while time.time() < deadline:
            # 1.5s 一次：手机端点完「确认登录」后，登录态落地越快被捕获的概率越高
            await page.wait_for_timeout(1500)
            tick += 1
            cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
            if cookies.get("sessionid"):
                kept = {k: v for k, v in cookies.items() if k in KEEP and v}
                path = save_account(name, kept)
                print(f"\n✅ 登录成功（等待 {tick * 1.5:.0f}s）")
                print(f"   账号名：{name}")
                print(f"   cookie：{', '.join(kept.keys())}")
                print(f"   凭证文件：{path}")
                await browser.close()
                return 0
            # 扫码后网页端可能弹二次确认
            try:
                c = await page.evaluate(CLICK_CONFIRM_JS)
                if c:
                    print(f"   → 已点「{c}」")
            except Exception:
                pass
            # 二维码有有效期，定时重截。
            # 注意：不要在这里无条件 close_promo —— shadow 里的「关闭」按钮可能是登录弹窗自己的 X，
            # 会把登录弹窗关掉。只在截不到登录弹窗（说明被推广窗盖住了）时才处理。
            if tick % 5 == 0:
                if not await shoot_qr(page, args.qr_out):
                    if await close_promo(page):
                        await open_login_and_scan_tab(page)
                        await shoot_qr(page, args.qr_out)
                    print(f"   （二维码已刷新，最新截图：{args.qr_out}）")

        print(f"\n⏱ 超时（{args.timeout}s）未检测到登录。二维码截图：{args.qr_out}")
        await browser.close()
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
