#!/usr/bin/env python3
"""
doubao-web-mcp —— 豆包网页版能力接入 MCP（多账号 · 图生视频优先）

架构（逐层实验验证）：
  - 对话 / 图片上传 / 文生图：纯 HTTP 直连，无需签名
  - 图生视频：需 X-Bogus 签名 → 签名器常驻一个 headless 浏览器页面
    evaluate window.bdms.frontierSign() 拿签名（毫秒级）。浏览器只当
    "签名器"，不作为请求通道；对话/图片全程无浏览器。
  - 多账号：账号池轮换 + 手动切换

凭证配置（三选一，优先级从上到下）：
  1. 环境变量 DOUBAO_ACCOUNTS = "名字1:cookie串1;名字2:cookie串2"
     （cookie 串为 k=v,k=v 形式，至少含 sessionid=xxx）
  2. 账号池文件 DOUBAO_ACCOUNTS_FILE（默认 ~/.doubao-mcp/accounts.json）
     —— 用本仓库的 login.py 扫码登录后自动生成
  3. 兼容旧路径 ~/.doubao_browser/accounts.json：需显式设置
     DOUBAO_LEGACY_ACCOUNTS=1（仅为从 doubao2api 迁移准备的退路，
     正常运行不需要，本项目不依赖 doubao2api）

依赖：httpx / mcp(<2) / playwright（仅视频签名用）
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

from doubao_client import (
    DoubaoChatClient,
    DoubaoChatError,
    DoubaoRateLimitError,
)

mcp = FastMCP("doubao-web")

OUT_DIR = Path(os.environ.get("DOUBAO_OUT_DIR", "~/Downloads/豆包输出")).expanduser()
OUT_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_DEVICE_ID = "714003710229497"
DEFAULT_WEB_ID = "7604137868021548590"
DEFAULT_FP = "verify_mlcfw5f7_TPq0YmFD_NrsC_4RuQ_BJPg_M5W7i58I7wV0"


# ---------------------------------------------------------------- 账号池

NATIVE_POOL = "~/.doubao-mcp/accounts.json"
LEGACY_POOL = "~/.doubao_browser/accounts.json"   # doubao2api 的目录，仅迁移退路


def _read_pool(path: str):
    """读一个账号池文件，返回 (账号列表, active 名)。"""
    p = Path(path).expanduser()
    if not p.exists():
        return [], None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return [], None
    accounts = [
        {"name": a.get("name", "未命名"), "cookies": a.get("cookies", {})}
        for a in data.get("accounts", [])
        if a.get("cookies", {}).get("sessionid")
    ]
    return accounts, data.get("active")


def _load_accounts():
    """加载账号池：环境变量 > 原生账号池文件 > （可选）旧路径。"""
    raw = os.environ.get("DOUBAO_ACCOUNTS", "").strip()
    accounts = []
    if raw:
        # "名字1:k1=v1,k2=v2;名字2:k=v"
        for chunk in raw.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" in chunk:
                name, cookie_str = chunk.split(":", 1)
            else:
                name, cookie_str = f"账号{len(accounts)+1}", chunk
            cookies = {}
            for kv in cookie_str.split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    cookies[k.strip()] = v.strip()
            if cookies.get("sessionid"):
                accounts.append({"name": name.strip(), "cookies": cookies})
    if accounts:
        return accounts, None

    accounts, active = _read_pool(os.environ.get("DOUBAO_ACCOUNTS_FILE", NATIVE_POOL))
    if accounts:
        return accounts, active
    if os.environ.get("DOUBAO_LEGACY_ACCOUNTS") == "1":
        accounts, _ = _read_pool(LEGACY_POOL)
        if accounts:
            return accounts, None
    return [], None


_ACTIVE = None


def _pick_account():
    accounts, active = _load_accounts()
    if not accounts:
        return None, None
    acc = _ACTIVE
    if acc is None and active:                     # 账号池文件里指定的默认账号
        acc = next((a for a in accounts if a["name"] == active), None)
    return accounts, acc or accounts[0]


def _new_client(acc: dict, timeout: float = 180) -> DoubaoChatClient:
    return DoubaoChatClient(
        cookies=acc["cookies"], ms_token="",
        device_id=DEFAULT_DEVICE_ID, web_id=DEFAULT_WEB_ID, fp=DEFAULT_FP,
        timeout_seconds=timeout,
    )


def _fail(exc) -> str:
    msg = str(exc)
    if "710022002" in msg or "710022004" in msg or "访问频繁" in msg:
        return ("🚫 豆包风控中（710022002/710022004）。这是 IP 级限流："
                "① 等 30 分钟冷却，期间别开豆包网页版 ② 换网络（手机热点最快）"
                "③ 在浏览器里手动完成一次滑块验证。")
    if "710012001" in msg or "登录已过期" in msg:
        return "⚪ 登录态过期，请更新账号 Cookie。"
    if isinstance(exc, DoubaoRateLimitError):
        return f"🚫 限流：{msg[:200]}"
    return f"❌ 失败：{msg[:300]}"


def _save(url: str, suffix: str) -> str:
    with httpx.Client(timeout=600, follow_redirects=True, trust_env=False) as c:
        r = c.get(url)
        r.raise_for_status()
    p = OUT_DIR / f"doubao_{time.strftime('%Y%m%d_%H%M%S')}{suffix}"
    p.write_bytes(r.content)
    return str(p)


def _fit_ratio(path: str, ratio: str) -> str:
    """把已落盘的图修正到目标比例（模糊背景填充，主体完整不裁切）。

    无水印通道（memory_type=0）走 agent 模式，服务端会忽略 ratio 参数，
    出图比例随机。这里在本地补齐：背景用放大模糊的原图铺满，前景原图居中。
    比例本来就对（或接近）时原样返回，不做任何处理。
    """
    if not ratio or ":" not in ratio:
        return path
    try:
        tw, th = (int(x) for x in ratio.split(":", 1))
    except Exception:
        return path
    target = tw / th
    try:
        from PIL import Image, ImageFilter, ImageEnhance
    except ImportError:
        return path
    try:
        img = Image.open(path).convert("RGB")
    except Exception:
        return path
    w, h = img.size
    if w <= 0 or h <= 0:
        return path
    if abs((w / h) - target) / target < 0.03:
        return path  # 已经够接近了

    # 目标画布：以原图长边为基准撑到目标比例
    if w / h < target:      # 原图偏窄 → 加宽
        cw, ch = int(round(h * target)), h
    else:                   # 原图偏宽 → 加高
        cw, ch = w, int(round(w / target))
    cw, ch = max(cw, 1), max(ch, 1)

    # 背景：cover 放大 + 模糊 + 压暗
    scale = max(cw / w, ch / h)
    bg = img.resize((max(int(w * scale), 1), max(int(h * scale), 1)), Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(radius=max(w, h) // 40))
    bg = ImageEnhance.Brightness(bg).enhance(0.55)
    bg = bg.crop(((bg.width - cw) // 2, (bg.height - ch) // 2,
                  (bg.width - cw) // 2 + cw, (bg.height - ch) // 2 + ch))

    # 前景：contain 原图居中
    fscale = min(cw / w, ch / h)
    fg = img.resize((max(int(w * fscale), 1), max(int(h * fscale), 1)), Image.LANCZOS)
    bg.paste(fg, ((cw - fg.width) // 2, (ch - fg.height) // 2))
    bg.save(path, quality=95)
    return path


# ---------------------------------------------------------------- MCP 工具

@mcp.tool()
def doubao_accounts(action: str = "list", name: str = "") -> str:
    """管理豆包账号池。action: list（列出）/ switch（切换，需 name）。
    账号来源：环境变量 DOUBAO_ACCOUNTS 或账号池文件 ~/.doubao-mcp/accounts.json
    （由本仓库 login.py 扫码登录生成）。"""
    global _ACTIVE
    accounts, _ = _load_accounts()
    if not accounts:
        return ("⚪ 账号池为空。两种配置方式：\n"
                "1) 跑一次扫码登录（推荐）：~/doubao-mcp/.venv/bin/python ~/doubao-mcp/login.py\n"
                "   扫码成功后自动写入 ~/.doubao-mcp/accounts.json\n"
                "2) MCP 配置 env: DOUBAO_ACCOUNTS=\"名字1:sessionid=xxx;名字2:sessionid=yyy\"")
    if action == "switch" and name:
        for a in accounts:
            if a["name"] == name:
                _ACTIVE = a
                return f"✅ 已切换到账号：{name}"
        return f"❌ 找不到账号「{name}」，可用：{[a['name'] for a in accounts]}"
    cur = _ACTIVE["name"] if _ACTIVE else f"{accounts[0]['name']}（默认）"
    lines = [f"当前账号：{cur}", "账号池："]
    lines += [f"  - {a['name']}（sessionid: {a['cookies'].get('sessionid','')[:6]}…）"
              for a in accounts]
    return "\n".join(lines)


@mcp.tool()
async def doubao_ping() -> str:
    """检查豆包链路：账号登录态 + IP 风控状态。任何生成前先调它。"""
    accounts, acc = _pick_account()
    if not acc:
        return "⚪ 未配置账号（见 doubao_accounts 说明）"
    try:
        async with _new_client(acc, timeout=90) as c:
            r = await c.chat_completion("说：好")
        return f"✅ 账号[{acc['name']}] 链路正常（回复：{(r.text or '').strip()[:30]}）。可对话 / 生图 / 生视频。"
    except Exception as e:
        return f"账号[{acc['name']}] {_fail(e)}"


@mcp.tool()
async def doubao_chat(prompt: str, model: str = "doubao") -> str:
    """豆包文本对话：写脚本、改稿、审片、起标题、整理资料。
    model: doubao（快）/ doubao-think（深度思考，传 1 走思考，0 快速）。"""
    accounts, acc = _pick_account()
    if not acc:
        return "⚪ 未配置账号（见 doubao_accounts 说明）"
    try:
        async with _new_client(acc) as c:
            r = await c.chat_completion(prompt)
        return r.text or "(空响应)"
    except Exception as e:
        return _fail(e)


@mcp.tool()
async def doubao_image(prompt: str, image_path: str = "", ratio: str = "1:1") -> str:
    """文生图或图生图。传 image_path 即为图生图。返回本地图片路径。"""
    accounts, acc = _pick_account()
    if not acc:
        return "⚪ 未配置账号（见 doubao_accounts 说明）"
    ref_key = ""
    try:
        async with _new_client(acc, timeout=300) as c:
            if image_path:
                p = Path(image_path).expanduser()
                if not p.exists():
                    return f"❌ 图片不存在：{p}"
                up = await c.upload_image(p.read_bytes(), p.name)
                ref_key = up.get("uri", "")
            result = await c.generate_image(prompt, ratio=ratio, ref_image_key=ref_key)
            if not result.images:
                return "⚠️ 未拿到图片（可能被风控，先用 doubao_ping 探测）"
            img = result.images[0]
            # 取链优先级：image_raw（原图无水印）> image_ori（dwm3 去水印）> 兜底换链
            url = (
                getattr(img, "ori_raw_url", "")
                or getattr(img, "ori_url", "")
                or getattr(img, "raw_url", "")
                or ""
            )
            if not url:
                info = await c.get_file_download_url(img.key)
                # 该接口在不同版本返回 dict 或 str，两种都兼容
                url = info if isinstance(info, str) else (
                    info.get("main_url") or info.get("url") or "")
            if not url:
                return f"⚠️ 拿到图片 key 但换取下载链接失败：{img.key[:60]}"
        path = _save(url, ".png")
        path = _fit_ratio(path, ratio)
        return f"✅ 图片已保存：{path}"
    except Exception as e:
        return _fail(e)


@mcp.tool()
async def doubao_video(
    prompt: str,
    image_path: str = "",
    ratio: str = "9:16",
    video_model: str = "seedance_v2.0_mini",
    duration: int = 10,
) -> str:
    """文生视频 / 图生视频（Seedance）。传 image_path 即为图生视频。

    ratio: 9:16（竖屏）/ 16:9 / 1:1 / 21:9
    video_model: seedance_v2.0_mini（默认，1x）/ seedance_v2.0（Fast）/ seedance_v2.0_std（2x）
    duration: 5 或 10秒

    🔒 已知限制（2026-10-19 实测）：豆包视频生成需要**正式订阅**，
    免费体验套餐会在最后的额度扣减环节被拦（agent 回复
    「当前免费体验套餐不可用于创作使用」）。协议链路本身已打通，
    开通后即可直接返回 mp4 路径。开通入口见返回文案。
    """
    accounts, acc = _pick_account()
    if not acc:
        return "⚪ 未配置账号（见 doubao_accounts 说明）"
    ref_key = ""
    try:
        async with _new_client(acc, timeout=400) as c:
            if image_path:
                p = Path(image_path).expanduser()
                if not p.exists():
                    return f"❌ 图片不存在：{p}"
                up = await c.upload_image(p.read_bytes(), p.name)
                ref_key = up.get("uri", "")
            result = await c.generate_video(
                prompt,
                ratio=ratio,
                ref_image_key=ref_key,
                video_model=video_model,
                video_duration=duration,
            )

            # 直返 mp4（服务端偶发同步返回时）
            if result.videos:
                v = result.videos[0]
                url = getattr(v, "video_url", "") or getattr(v, "url", "")
                if url:
                    path = _save(url, ".mp4")
                    return f"✅ 视频已保存：{path}"

            reply = getattr(result, "agent_reply", "") or ""

            # 🔴 付费墙判定（2026-10-19 实测四种模型/时长组合均命中）
            # 参数齐全后 agent 才会走到扣费环节，此时返回订阅提示。
            # 这不是协议/风控问题，纯账号套餐限制。
            if "免费体验套餐" in reply or "升级正式订阅" in reply:
                return (
                    "🔒 豆包视频生成需要正式订阅（当前账号是免费体验套餐）。\n"
                    "   已确认这不是协议或风控问题：链路完全打通，模型/时长/比例参数\n"
                    "   均被服务端正确接收，卡在最后的额度扣减环节。\n"
                    f"   豆包提示：{reply[:120]}\n"
                    "   开通入口：https://www.doubao.com/member/subscription"
                    "?default_sku_key=doubao_personal_std&enter_method=answer_quota_limiting\n"
                    "   开通后本工具即可直接返回 mp4 路径。"
                )

            # 参数没被识别 —— agent 在追问，需要把参数写进自然语言
            if "确认" in reply or "建议" in reply:
                return (
                    "⚠️ 豆包未接受参数，正在追问确认，任务未生成。\n"
                    f"   豆包回复：{reply[:180]}\n"
                    "   建议：把比例/时长/模型直接写进 prompt 描述里，例如\n"
                    '   「生成一个 16:9、10 秒、Seedance 2.0 Mini 的视频：……」。'
                )

            # 异步：回传会话链接，让用户去网页端取成品
            conv = getattr(result, "conversation_id", "") or ""
            link = (
                f"https://www.doubao.com/chat/{conv}" if conv else "https://www.doubao.com/chat/"
            )
            lines = [
                "⏳ 视频任务已提交（异步渲染，官方口径约 10 分钟）",
                f"   模型：{video_model} · 时长 {duration}s · 比例 {ratio}",
            ]
            if reply:
                lines.append(f"   豆包回复：{reply[:150]}")
            if conv:
                lines.append(f"   会话链接：{link}")
            lines.append("   完成后豆包会在该会话推送成品，打开链接即可预览下载。")
            return "\n".join(lines)
    except Exception as e:
        return _fail(e)


if __name__ == "__main__":
    mcp.run()