#!/usr/bin/env python3
"""
doubao-web-mcp —— 豆包网页版能力接入 MCP（多账号 · 模型选择）

架构（逐层实验验证）：
  - 对话 / 文生图 / 文生视频：纯 HTTP 直连 doubao.com，全程免签名
    （X-Bogus 签名已失效，加签名反而触发 710022004 风控，详见 README）
  - 视频是异步 agent 通道：memory_type=0 提交 → 轮询会话消息取回成品
  - 多账号：账号池轮换 + 手动切换

凭证配置（三选一，优先级从上到下）：
  1. 环境变量 DOUBAO_ACCOUNTS = "名字1:cookie串1;名字2:cookie串2"
     （cookie 串为 k=v,k=v 形式，至少含 sessionid=xxx）
  2. 账号池文件 DOUBAO_ACCOUNTS_FILE（默认 ~/.doubao-mcp/accounts.json）
     —— 用本仓库的 login.py 扫码登录后自动生成
  3. 兼容旧路径 ~/.doubao_browser/accounts.json：需显式设置
     DOUBAO_LEGACY_ACCOUNTS=1（仅为从 doubao2api 迁移准备的退路，
     正常运行不需要，本项目不依赖 doubao2api）

依赖：httpx / mcp(<2)
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
    IMAGE_MODELS,
    IMAGE_STYLES,
    VIDEO_MODELS,
)

# 无水印视频下载：优先纯 API（快），失败回退无头浏览器点面板（稳），最后带印原链接兜底。
try:
    from doubao_video_clean_dl import download_clean_video
except Exception:  # 模块缺失/依赖不全时不拖垮整个 MCP
    download_clean_video = None
try:
    from doubao_video_headless_dl import download_video_panel
except Exception:
    download_video_panel = None

# C 方案（治本）：优先用持久化浏览器 profile 提取的「浏览器维护会话」cookie，
# 没有 profile 的账号自动回退到 accounts.json 的静态 cookie（见 profile_cookies.py）。
try:
    from profile_cookies import get_fresh_cookies
except Exception:  # 模块缺失时不拖垮整个 MCP
    async def get_fresh_cookies(acc, ttl=300):
        return acc.get("cookies", {})

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


def _new_client(acc: dict, cookies=None, timeout: float = 180) -> DoubaoChatClient:
    cookies = cookies or acc.get("cookies", {})
    return DoubaoChatClient(
        cookies=cookies, ms_token="",
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
        cookies = await get_fresh_cookies(acc)
        async with _new_client(acc, cookies=cookies, timeout=90) as c:
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
        cookies = await get_fresh_cookies(acc)
        async with _new_client(acc, cookies=cookies) as c:
            r = await c.chat_completion(prompt)
        return r.text or "(空响应)"
    except Exception as e:
        return _fail(e)


@mcp.tool()
async def doubao_models(kind: str = "all") -> str:
    """查询豆包官方创作模型与风格清单（图片/视频）。

    kind: image / video / style / all（默认 all）
    返回官方 option_key（直接传给 doubao_image 的 image_model/style、
    doubao_video 的 video_model 参数），标注付费墙（需订阅）与消耗倍率。
    """
    lines = []
    if kind in ("image", "all"):
        lines.append("🖼 图片模型（doubao_image image_model=）")
        for key, info in IMAGE_MODELS.items():
            mark = " 🔒需订阅" if info.get("need_upgrade") else ""
            lines.append(f"  - {key}（{info.get('sub', '')}{mark}）")
        lines.append("")
    if kind in ("video", "all"):
        lines.append("🎬 视频模型（doubao_video video_model=）")
        for key, info in VIDEO_MODELS.items():
            mark = " 🔒需订阅" if info.get("need_upgrade") else ""
            lines.append(f"  - {key}（{info.get('display', key)}・{info.get('sub', '')}{mark}）")
        lines.append("")
    if kind in ("style", "all"):
        lines.append(f"🎨 图片风格（doubao_image style=，共 {len(IMAGE_STYLES)} 种）")
        for key, name in IMAGE_STYLES.items():
            lines.append(f"  - {key}（{name}）")
        lines.append("")
    lines.append("比例：图片 1:1/16:9/9:16/4:3/3:4/21:9；视频另支持 3:4/4:3")
    lines.append("视频时长：5 / 10 秒")
    return "\n".join(lines)


@mcp.tool()
async def doubao_image(
    prompt: str,
    image_path: str = "",
    ratio: str = "1:1",
    image_model: str = "",
    style: str = "",
) -> str:
    """文生图或图生图。传 image_path 即为图生图。返回本地图片路径。

    输出默认**无「豆包 AI 生成」水印**（走 memory_type=0 + image_raw/dwm3 取链）。
    ratio 由服务端按描述文本决定，这里会自动把目标比例写进描述，
    原生比例仍不符时本地用模糊背景补齐（主体不裁切）。

    ratio: 1:1 / 16:9 / 9:16 / 4:3 / 3:4 / 21:9
    image_model: 图片模型（留空=服务端默认 Seedream 4.0）：
      - Seedream 5.0 Flash（专业快速，推荐）
      - Seedream 5.0 Pro（专业出图・4 倍消耗，需订阅）
      - Seedream 4.5（日常生成）
      - Seedream 4.0（基础生图）
    style: 官方风格 option_key（32 种，完整列表见 doubao_models），
      如 skill_image_styles_film（电影写真）/ skill_image_styles_chinese（中国风）。
      会自动按官方规则注入「图片风格为X」到描述里。
    """
    accounts, acc = _pick_account()
    if not acc:
        return "⚪ 未配置账号（见 doubao_accounts 说明）"
    ref_key = ""
    try:
        cookies = await get_fresh_cookies(acc)
        async with _new_client(acc, cookies=cookies, timeout=300) as c:
            if image_path:
                p = Path(image_path).expanduser()
                if not p.exists():
                    return f"❌ 图片不存在：{p}"
                up = await c.upload_image(p.read_bytes(), p.name)
                ref_key = up.get("uri", "")
            result = await c.generate_image(
                prompt, ratio=ratio, ref_image_key=ref_key,
                image_model=image_model, style=style,
            )
            # 付费墙/额度提示（新协议服务端返回，不出图）
            notice = getattr(result, "notice", "") or ""
            if notice and not result.images:
                return (
                    f"🔒 {notice}\n"
                    f"   请求模型：{image_model or 'Seedream 4.0'}（需订阅档）\n"
                    "   建议：换免费档模型重试 ——\n"
                    "     Seedream 5.0 Flash（专业快速）/ Seedream 4.5（日常）/ Seedream 4.0（基础）\n"
                    "   或切换有订阅的账号（doubao_accounts）。"
                )
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
        extra = ""
        eff_model = getattr(result, "model", "") or image_model
        if eff_model:
            extra += f"\n   模型：{eff_model}"
        if style:
            extra += f"\n   风格：{IMAGE_STYLES.get(style, style)}"
        if notice:
            extra += f"\n   ⚠️ {notice}"
        return f"✅ 图片已保存：{path}{extra}"
    except Exception as e:
        return _fail(e)


async def _download_video_clean(v, acc: dict, conv_id: str):
    """内部 helper（不是 MCP 工具）：三级视频下载，优先拿无水印版：
      1) 纯 API（get_without_watermark?vid=…，最快）→ 2) 无头浏览器点面板（最稳）
      → 3) 带印原链接（MCP 原有行为兜底）。
    返回 (path, channel_label)。全部失败抛异常。
    """
    key = time.strftime("%Y%m%d_%H%M%S")
    base = str(OUT_DIR / f"doubao_{key}")
    out = base + ".mp4"
    vid = getattr(v, "vid", "") or ""
    cookies = await get_fresh_cookies(acc)

    # 1) 纯 API 无水印（优先、最快）
    if vid and download_clean_video is not None:
        try:
            await download_clean_video(vid, cookies, conv_id, out)
            if os.path.exists(out) and os.path.getsize(out) > 0:
                return out, "无水印(API)"
        except Exception as e:
            print(f"[clean-dl] API 无水印失败，转无头面板：{e}")

    # 2) 无头浏览器点面板下载（回退、稳）
    if download_video_panel is not None:
        try:
            if os.path.exists(out) and os.path.getsize(out) == 0:
                os.remove(out)
            await download_video_panel(vid, cookies, conv_id, out)
            if os.path.exists(out) and os.path.getsize(out) > 0:
                return out, "无水印(无头面板)"
        except Exception as e:
            print(f"[clean-dl] 无头面板失败，转带印原链接：{e}")

    # 3) 带印原链接兜底（MCP 原有行为）
    url = getattr(v, "video_url", "") or getattr(v, "url", "")
    if url:
        return _save(url, ".mp4"), "带水印(原链接)"
    raise RuntimeError(f"无可用视频下载链接 (vid={vid})")


@mcp.tool()
async def doubao_video(
    prompt: str,
    image_path: str = "",
    ratio: str = "9:16",
    video_model: str = "seedance_v2.0_mini",
    duration: int = 10,
    wait: int = 600,
) -> str:
    """文生视频 / 图生视频（Seedance）。传 image_path 即为图生视频。

    ratio: 9:16（竖屏）/ 16:9 / 1:1 / 21:9 / 3:4 / 4:3
    video_model（官方四档，详见 doubao_models）：
      - seedance_v2.0_mini（默认，日常生成，免费额度可跑）
      - seedance_v2.0（Fast，快速出片，免费额度可跑）
      - seedance_v2.0_std（2.0 进阶画面，2x 消耗，🔒需订阅）
      - seedance_v2.5（旗舰 30 秒，5x 消耗，🔒需订阅）
    duration: 5 或 10秒
    wait: 等待成品的秒数（默认 600=10 分钟，与官方预计等待时间匹配）；
          传 0 = 只提交任务，立即返回会话链接。

    输出默认**无「豆包 AI 生成」水印**：三级下载——
      ① 纯 API `get_without_watermark?vid=`（最快）
      ② 无头浏览器点右侧面板下载（稳，100% 干净）
      ③ 带印原链接兜底。
    返回文本里的「下载通道：」标明实际走了哪一级，看到「带水印(原链接)」＝前两级都失败。

    🔒 账号额度说明（2026-10-19 实测）：视频生成走新统一端点
    （/chat/completion + chat_ability ability_type=17），模型/时长/比例精确选择。
    提交后服务端先回「参数确认卡」，客户端自动二次回复「确认」，
    任务才真正进入渲染（异步，官方口径约 10 分钟），轮询会话取回成品。
    需要账号有视频创作额度——免费体验套餐会在扣费环节被拦
    （「当前免费体验套餐不可用于创作使用」）；有每日免费额度的账号
    （如账号池 ipone，Seedance 2.0 每日 10 次）可跑 Mini/Fast 两档。
    额度查询与账号切换见 doubao_accounts。
    """
    accounts, acc = _pick_account()
    if not acc:
        return "⚪ 未配置账号（见 doubao_accounts 说明）"
    ref_key = ""
    try:
        cookies = await get_fresh_cookies(acc)
        async with _new_client(acc, cookies=cookies, timeout=400) as c:
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
                poll_timeout=max(0, wait),
            )

            # 成品到手（轮询会话取回 or 服务端偶发同步返回）
            if result.videos:
                v = result.videos[0]
                conv_id = getattr(result, "conversation_id", "") or ""
                try:
                    path, channel = await _download_video_clean(v, acc, conv_id)
                except Exception as ex:
                    # 全部下载通道失败：最后再试一次原链接（最保守兜底）
                    url = getattr(v, "video_url", "") or getattr(v, "url", "")
                    if url:
                        path = _save(url, ".mp4")
                        channel = "带水印(原链接)"
                    else:
                        return f"❌ 视频下载失败：{ex}"
                extra = f"\n   vid：{v.vid}" if getattr(v, "vid", "") else ""
                extra += f"\n   下载通道：{channel}"
                eff = getattr(result, "model", "")
                if eff:
                    extra += f"\n   模型：{eff}"
                return f"✅ 视频已保存：{path}{extra}"

            reply = getattr(result, "agent_reply", "") or ""

            # 🔴 付费墙判定：套餐无视频创作额度 / 所选模型需订阅（账号问题，非协议问题）
            if ("免费体验套餐" in reply or "升级正式订阅" in reply
                    or "不可用于创作" in reply or "订阅标准套餐专属能力" in reply
                    or "无该视频模型" in reply):
                return (
                    "🔒 当前账号（"
                    + acc.get("name", "?")
                    + f"）无法使用所选模型（{video_model}）。\n"
                    "   免费档模型：seedance_v2.0_mini / seedance_v2.0（Fast）；\n"
                    "   需订阅档：seedance_v2.0_std（2x）/ seedance_v2.5（5x）。\n"
                    f"   豆包提示：{reply[:120]}\n"
                    "   解决：改用免费档模型，或切换有订阅的账号（doubao_accounts），\n"
                    "   或开通正式订阅：https://www.doubao.com/member/subscription"
                    "?default_sku_key=doubao_personal_std&enter_method=answer_quota_limiting"
                )

            # 🔴 当日免费次数用完：agent 回的是正常文案，会被下面的「⏳ 已提交」分支
            # 误判成任务在跑（2026-10-23 实测：一帧没渲，却返回会话链接像成功）。
            if "免费次数用完" in reply or ("今日" in reply and "用完" in reply):
                return (
                    f"🚫 账号（{acc.get('name', '?')}）今日视频免费次数已用完，"
                    "任务**未进入渲染**（不是提交成功）。\n"
                    f"   豆包提示：{reply[:120]}\n"
                    "   解决：切换其他有额度的账号（doubao_accounts action=switch）后重试，"
                    "或等次日额度恢复；开通订阅可免等待。"
                )

            # 风控拦截
            if "710022004" in reply or "风控" in reply:
                return (
                    "🚫 请求被豆包风控拦截（710022004）。\n"
                    f"   {reply[:200]}\n"
                    "   建议：稍后重试，或更换网络环境后重试。"
                )

            # 确认步骤失败（新协议两步流程的第二步行没走通）
            if "确认步骤失败" in reply:
                return (
                    "⚠️ 视频任务已提交，但自动「确认」步骤失败，渲染未开始。\n"
                    f"   详情：{reply[:200]}\n"
                    "   可手动打开会话链接回复「确认」，或稍后重试。"
                )

            # 参数没被识别（仅旧通道兜底时）—— agent 在追问
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
                f"   模型：{getattr(result, 'model', '') or video_model} · 时长 {duration}s · 比例 {ratio}",
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