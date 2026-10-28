# -*- coding: utf-8 -*-
"""
豆包视频「无水印下载」纯 API 备用方案
=====================================

背景：
  MCP 原本的视频下载走卡片 `download_url` / `video_list.video_1.main_url`，
  这些 URL 带 `lr=video_gen_watermark_dyn`，下载到的是**带"豆包AI生成"水印**的版本。
  豆包网页端「点视频 → 右侧面板 → 下载」拿到的却是无水印版，差异在于：

    带印播放/卡片 URL : ...&lr=video_gen_watermark_dyn
    面板下载干净 URL   : ...&lr=unwatermarked   （同一视频、完全不同的签名 URL）

  干净 URL 由 `POST /creativity/resource/get_without_watermark` 返回。
  浏览器靠 URL 的 Referer(conversation_id) 隐式定位视频；纯 API 复刻时必须把
  `vid` 作为 **query 参数**显式传入，才能返回 `without_watermark=true` + 干净 URL。

关键坑（已逐一实测排雷）：
  1. vid 必须放 **query 参数**（`?vid=xxx`），放 body 无效（body 传 vid 返回 710010202）。
     参数名是 `vid`（不是 resource_id / item_id / video_id）。
  2. cookie 必须用字符串直接塞进 `Cookie` 请求头。aiohttp 的
     `ClientSession(cookies=dict)` 因缺 domain 不会发送 sessionid，导致接口返回 false。
  3. 需要带 `Referer: https://www.doubao.com/chat/{conversation_id}`（aid 用视频端点 497858）。
  4. **不需要**任何会话初始化（GET 聊天页 / latest_messagelist 都不必），单发这一条即可。
  5. 干净 URL 是带鉴权的 CDN 签名链接，直接用普通 HTTP 流式下载即可，无需 cookie。
     下载用流式 chunk，避免 aiohttp 默认 read_bufsize 读不完大文件（ContentLengthError）。

用法：
  from doubao_video_clean_dl import download_clean_video
  path = await download_clean_video(vid, cookies_dict, conversation_id, "out.mp4")

  其中 cookies_dict 即 accounts.json 里某账号的 cookies（name->value 字典）。
"""
import asyncio
import json
import os
import re
import uuid
import aiohttp

# 视频端点通用参数（aid=497858，与网页端一致；不要误用对话端点的 582478）
_SEC = {
    "version_code": "20800", "language": "zh", "device_platform": "web",
    "doubao_device_platform": "web", "aid": "497858", "real_aid": "497858",
    "pkg_type": "release_version", "device_id": "7699777196361844259",
    "pc_version": "3.42.3", "doubao_pc_version": "3.42.3",
    "web_id": "7699777216913720875", "tea_uuid": "7699777216913720875",
    "region": "CN", "sys_region": "CN", "samantha_web": "1",
    "web_platform": "browser", "use-olympus-account": "1",
}
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
_ENDPOINT = "https://www.doubao.com/creativity/resource/get_without_watermark"


def _cookie_str(cookies) -> str:
    if isinstance(cookies, dict):
        return "; ".join(f"{k}={v}" for k, v in cookies.items())
    return "; ".join(f"{c.get('name')}={c.get('value')}" for c in cookies)


def _lr(url: str) -> str:
    m = re.search(r"lr=([^&]+)", url or "")
    return m.group(1) if m else "?"


async def get_clean_video_url(vid: str, cookies, conversation_id: str) -> str:
    """拿无水印视频的签名下载 URL。失败返回空串。"""
    cookie_str = _cookie_str(cookies)
    params = dict(_SEC)
    params["web_tab_id"] = str(uuid.uuid4())
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    ref = f"https://www.doubao.com/chat/{conversation_id}"
    url = f"{_ENDPOINT}?{qs}&vid={vid}"
    headers = {
        "User-Agent": _UA, "Referer": ref,
        "Content-Type": "application/json", "Cookie": cookie_str,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=headers, data="{}") as r:
            d = await r.json()
    data = d.get("data") or {}
    if not data.get("without_watermark"):
        return ""
    dw = data.get("download_video") or {}
    for vv in dw.values():
        u = vv.get("download_url") or ""
        if u:
            return u
    return ""


async def download_clean_video(
    vid: str, cookies, conversation_id: str, out_path: str, chunk_size: int = 1 << 16
) -> str:
    """下载无水印视频到 out_path，返回路径；失败抛异常。"""
    url = await get_clean_video_url(vid, cookies, conversation_id)
    if not url:
        raise RuntimeError(f"get_without_watermark 未返回干净 URL (vid={vid})")
    cookie_str = _cookie_str(cookies)
    ref = f"https://www.doubao.com/chat/{conversation_id}"
    headers = {"User-Agent": _UA, "Referer": ref, "Cookie": cookie_str}
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                async for chunk in r.content.iter_chunked(chunk_size):
                    f.write(chunk)
    return out_path


if __name__ == "__main__":
    # 自测：用 /tmp/doubao_target.json 里现成的会话 + 视频
    import subprocess
    t = json.load(open("/tmp/doubao_target.json", encoding="utf-8"))
    cid, vid = t["conversation_id"], t["vid"]
    pool = json.load(open(os.path.expanduser("~/.doubao-mcp/accounts.json"), encoding="utf-8"))
    acc = next(a for a in pool["accounts"] if a["name"] == pool.get("active"))
    out = "/tmp/doubao_clean_module_test.mp4"
    path = asyncio.run(download_clean_video(vid, acc["cookies"], cid, out))
    print("saved ->", path, os.path.getsize(path), "bytes")
    # 抽两帧供人工核对
    for ts, o in [("2.3", "/tmp/doubao_clean_module_f1.png"), ("4.6", "/tmp/doubao_clean_module_f2.png")]:
        subprocess.run(["ffmpeg", "-y", "-ss", ts, "-i", path, "-frames:v", "1", "-q:v", "2", o],
                        capture_output=True)
        print("frame", o, os.path.getsize(o))
