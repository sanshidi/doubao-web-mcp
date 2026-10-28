# -*- coding: utf-8 -*-
"""
豆包视频「无水印下载」无头浏览器回退方案
=========================================

当纯 API 方案（doubao_video_clean_dl.download_clean_video）拿不到干净 URL 时，
用 Playwright 注入账号登录态 -> 进会话 -> 点视频 -> 右侧面板 -> 点蓝色下载按钮
-> 落盘。这条链路拿到的是 `lr=unwatermarked` 的独立签名文件，确认无水印。

与 doubao_video_clean_dl 的区别：
  - clean_dl 是纯 HTTP（快、无浏览器开销），优先用；
  - 本模块是浏览器回退（重、慢，但 100% 拿干净版）。

注意：本模块只暴露 async 函数，不在 import 时启动浏览器，可在 MCP 的 asyncio
事件循环内直接 await。手动调试用 `python doubao_video_headless_dl.py` 跑 __main__。
"""
import asyncio
import json
import os
import sys

from playwright.async_api import async_playwright

POOL = os.path.expanduser("~/.doubao-mcp/accounts.json")
DL_DIR = "/tmp/doubao_dl"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")


def _normalize_cookies(cookies):
    """接受 dict(name->value) 或 playwright cookie list，统一成 add_cookies 需要的 list。"""
    if isinstance(cookies, list):
        out = []
        for c in cookies:
            nc = dict(c)
            if "domain" not in nc:
                nc["domain"] = ".doubao.com"
            if "path" not in nc:
                nc["path"] = "/"
            out.append(nc)
        return out
    # dict -> playwright cookie list
    return [{"name": k, "value": v, "domain": ".doubao.com", "path": "/"}
            for k, v in (cookies or {}).items()]


_VIDEO_BOX_JS = """() => {
    // 优先找视频卡容器（class 含 video-player/video-card/video-wrapper）
    const cardSels = ['[class*=video-player]', '[class*=video-card]', '[class*=video-wrapper]', '[class*=videoItem]'];
    for (const sel of cardSels) {
        const els = Array.from(document.querySelectorAll(sel));
        for (const el of els) {
            const r = el.getBoundingClientRect();
            if (r.width > 150 && r.height > 150) {
                return {x: r.x + r.width/2, y: r.y + r.height/2, w: r.width, h: r.height, kind: 'card:' + sel};
            }
        }
    }
    // 退而求其次：大图（>150px，排除 98px 头像！）
    let best = null, ba = 0;
    document.querySelectorAll('img').forEach(im => {
        const s = im.currentSrc || im.src || '';
        if (/tos-cn|vdl|imagex|byteimg/.test(s)) {
            const r = im.getBoundingClientRect();
            if (r.width < 150 || r.height < 150) return;
            const a = r.width * r.height;
            if (a > ba) { ba = a; best = {x: r.x + r.width/2, y: r.y + r.height/2, w: r.width, h: r.height, kind: 'img'}; }
        }
    });
    if (best) return best;
    // 最后：video 元素
    const v = document.querySelector('video');
    if (v && v.getBoundingClientRect().width > 50) {
        const r = v.getBoundingClientRect();
        return {x: r.x + r.width/2, y: r.y + r.height/2, w: r.width, h: r.height, kind: 'video'};
    }
    return null;
}"""

_TOPBAR_JS = """() => {
    const out = [];
    document.querySelectorAll('button').forEach(e => {
        const r = e.getBoundingClientRect();
        if (r.width < 4 || r.height < 4) return;
        if (r.y > 70) return;
        const txt = (e.innerText||'').trim().replace(/\\s+/g,' ');
        const al = e.getAttribute('aria-label')||'';
        const cls = (e.className&&e.className.toString?e.className.toString():'');
        out.push({x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2),
            w:Math.round(r.width), txt:txt.slice(0,20), al:al.slice(0,20), cls:cls.slice(0,60)});
    });
    return out.filter(o=>o.x>600).sort((a,b)=>a.x-b.x);
}"""

_DOWNLOAD_CLICK_JS = """() => {
    // 顶栏下载键：蓝色实心小圆钮（rgb(0,102,255) 类）。排除「下载电脑版」推广宽按钮（会下到安装包！）
    const btns = Array.from(document.querySelectorAll('button')).filter(b=>{
        const r=b.getBoundingClientRect();
        if (r.width<10 || r.height<10 || r.y>=70 || r.x<600) return false;
        if (r.width>60) return false;  // 排除宽带推广钮
        const txt=(b.innerText||'').trim();
        if (/电脑版|客户端|App|APP/.test(txt)) return false;
        return true;
    });
    if (!btns.length) return {ok:false, n:0};
    // 优先：蓝色实心钮（bg 为蓝系：b>150 且 r<120）
    const blue = btns.filter(b=>{
        const m = getComputedStyle(b).backgroundColor.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
        if (!m) return false;
        const [r, g, bl] = [ +m[1], +m[2], +m[3] ];
        return bl > 150 && r < 120 && bl > r + 60;
    });
    let tgt, method;
    if (blue.length) {
        blue.sort((a,b)=> (b.getBoundingClientRect().x)-(a.getBoundingClientRect().x));
        tgt = blue[0]; method = 'blue-filled';
    } else {
        const filled = btns.filter(b=>{
            const c=(b.className&&b.className.toString?b.className.toString():'');
            return /bg-|transition-colors/.test(c);
        });
        const pool = filled.length ? filled : btns;
        pool.sort((a,b)=> (b.getBoundingClientRect().x)-(a.getBoundingClientRect().x));
        tgt = pool[0]; method = filled.length ? 'bg-filled' : 'rightmost-small';
    }
    const r=tgt.getBoundingClientRect();
    tgt.click();
    return {ok:true, method, x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2), w:Math.round(r.width), n:btns.length};
}"""

# 面板是否打开：出现大 video 元素（宽>300）即视为预览面板已开
_PANEL_OPEN_JS = """() => {
    return Array.from(document.querySelectorAll('video')).some(v => v.getBoundingClientRect().width > 300);
}"""

# 视频卡滚动进视口（负坐标/视口外时必须先滚）
_SCROLL_VIDEO_JS = """() => {
    const v = document.querySelector('video');
    const el = v || Array.from(document.querySelectorAll('img')).find(im=>{
        const s=im.currentSrc||im.src||'';
        return /tos-cn|vdl|imagex|byteimg/.test(s) && im.getBoundingClientRect().width>50;
    });
    if (el && el.scrollIntoView) { el.scrollIntoView({block:'center', inline:'center'}); return true; }
    return false;
}"""


async def download_video_panel(vid, cookies, conversation_id, out_path,
                                wait_video: float = 8.0,
                                click_timeout: float = 40.0,
                                attempts: int = 3):
    """无头点面板下载（回退方案）。返回落盘路径；失败抛异常。

    vid 仅用于日志/未来匹配；本函数按 conversation_id 进入会话并点击首个视频，
    不依赖 vid 精确定位。
    """
    pw_cookies = _normalize_cookies(cookies)
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    url = f"https://www.doubao.com/chat/{conversation_id}"
    print(f"[headless-dl] 进会话 {conversation_id}，目标 {out_path}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, channel="chrome",
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900}, user_agent=UA)
        try:
            await ctx.add_cookies(pw_cookies)
        except Exception as ex:
            print(f"[headless-dl] add_cookies warn: {ex}")
        page = await ctx.new_page()

        # 导航（带重试，SPA 偶发不挂载）
        nav_ok = False
        for att in range(4):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as ex:
                print(f"[headless-dl] GOTO warn(att{att+1}): {str(ex)[:120]}")
            await page.wait_for_timeout(3000)
            if "doubao.com" in page.url:
                nav_ok = True
                break
        if not nav_ok:
            await browser.close()
            raise RuntimeError("无头导航到会话失败（可能被踢回登录）")

        # 等视频渲染
        try:
            await page.wait_for_function("""() => {
                const v = document.querySelector('video');
                if (v && v.getBoundingClientRect().width>50) return true;
                return Array.from(document.querySelectorAll('img')).some(im=>{
                    const s=im.currentSrc||im.src||'';
                    return /tos-cn|vdl|imagex|byteimg/.test(s) && im.getBoundingClientRect().width>50;
                });
            }""", timeout=45000)
            print("[headless-dl] 视频已渲染")
        except Exception as ex:
            print(f"[headless-dl] 视频未渲染 warn: {str(ex)[:120]}")
        await page.wait_for_timeout(int(wait_video * 1000))

        downloaded = False
        for att in range(attempts):
            print(f"[headless-dl] download attempt {att+1}/{attempts}")
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await page.wait_for_timeout(1200)
            # 点视频卡 -> 打开右侧面板（locator 自动滚动，解决视口外负坐标）
            panel_opened = False
            try:
                card = page.locator(
                    '[class*=video-player], [class*=video-card], [class*=video-wrapper]').first
                if await card.count() > 0:
                    await card.scroll_into_view_if_needed(timeout=8000)
                    await page.wait_for_timeout(500)
                    await card.click(timeout=8000)
                    print("[headless-dl] CLICK video card (locator)")
                else:
                    box = await page.evaluate(_VIDEO_BOX_JS)
                    if box:
                        await page.mouse.click(box["x"], box["y"])
                        print(f"[headless-dl] CLICK video ({box['x']:.0f},{box['y']:.0f}) {box.get('kind','')}")
                    else:
                        print("[headless-dl] 未定位视频，重试")
                        await page.wait_for_timeout(3000)
                        continue
            except Exception as ex:
                print(f"[headless-dl] click video err: {str(ex)[:120]}")
            # 等面板打开（大 video 元素出现）
            for _ in range(10):
                await page.wait_for_timeout(700)
                try:
                    if await page.evaluate(_PANEL_OPEN_JS):
                        panel_opened = True
                        break
                except Exception:
                    pass
            if not panel_opened:
                print("[headless-dl] 面板未打开（无大 video 元素），重试")
                await page.wait_for_timeout(2000)
                continue
            print("[headless-dl] 面板已打开")
            # 🔴 关键修复：expect_download 必须包住点击（旧版先点后监听，事件必错过）
            # 🔴 单位修复：Playwright timeout 是毫秒，参数是秒，须 ×1000（旧版 40.0=40ms 必超时）
            try:
                async with page.expect_download(timeout=click_timeout * 1000) as dl_info:
                    info = await page.evaluate(_DOWNLOAD_CLICK_JS)
                    print(f"[headless-dl] CLICK download: {info}")
                download = await dl_info.value
                await download.save_as(out_path)
                sz = os.path.getsize(out_path) if os.path.exists(out_path) else 0
                # 防呆：抓到安装包（>20MB）立即判失败
                if sz > 20 * 1024 * 1024:
                    print(f"[headless-dl] 文件过大({sz/1048576:.0f}MB疑似安装包)，判失败")
                    if os.path.exists(out_path):
                        os.unlink(out_path)
                    raise RuntimeError("下载到非视频文件（疑似电脑版安装包）")
                print(f"[headless-dl] DOWNLOADED -> {out_path} ({sz} bytes)")
                downloaded = True
                break
            except Exception as ex:
                print(f"[headless-dl] attempt {att+1} 失败: {str(ex)[:140]}")
        await browser.close()
    if not downloaded:
        raise RuntimeError("无头面板下载 3 次均失败")
    return out_path


if __name__ == "__main__":
    t = json.load(open("/tmp/doubao_target.json", encoding="utf-8"))
    cid, vid = t["conversation_id"], t["vid"]
    pool = json.load(open(POOL, encoding="utf-8"))
    acc = next(a for a in pool["accounts"] if a["name"] == pool.get("active"))
    out = "/tmp/doubao_dl/headless_test.mp4"
    path = asyncio.run(download_video_panel(vid, acc["cookies"], cid, out))
    print("saved ->", path, os.path.getsize(path), "bytes")
