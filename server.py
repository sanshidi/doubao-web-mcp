#!/usr/bin/env python3
"""
豆包 MCP —— 把本机 doubao2api（127.0.0.1:9090）包装成 MCP 工具。

暴露四个工具：ping / chat / image / video。
所有生成结果自动落盘到本机目录并返回绝对路径，方便后续直接处理。

环境变量：
  DOUBAO_API_BASE  豆包服务地址（默认 http://127.0.0.1:9090）
  DOUBAO_OUT_DIR   生成文件保存目录（默认 ~/Downloads/豆包输出）
"""

import os
import time
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

API = os.environ.get("DOUBAO_API_BASE", "http://127.0.0.1:9090").rstrip("/")
OUT_DIR = Path(os.environ.get("DOUBAO_OUT_DIR", "~/Downloads/豆包输出")).expanduser()
OUT_DIR.mkdir(parents=True, exist_ok=True)

mcp = FastMCP("doubao")

RISK_HINT = (
    "🚫 豆包 IP 风控中（错误码 710022002「当前服务访问频繁」）。\n"
    "这是豆包对当前公网 IP 的限流，不是账号或部署问题。\n"
    "解决：① 等 30 分钟冷却，期间别开豆包网页版；\n"
    "      ② 换网络（手机热点最快），然后重启服务 ~/doubao2api/start_server.sh。"
)


def _client(timeout: float) -> httpx.Client:
    # trust_env=False：不继承系统代理，避免 127.0.0.1 被代理劫持
    return httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False)


def _fail(resp_text: str) -> str:
    if "710022002" in resp_text or "访问频繁" in resp_text:
        return RISK_HINT
    if "Browser client failed" in resp_text:
        return ("⚠️ 浏览器实例已断连。回终端 Ctrl+C 停掉服务，再跑 "
                "~/doubao2api/start_server.sh（若残留僵尸浏览器："
                "pkill -f 'user-data-dir=/Users/edy/.doubao_browser'）")
    return f"❌ 接口错误：{resp_text[:400]}"


def _save(url: str, suffix: str) -> str:
    with _client(600) as c:
        r = c.get(url)
        r.raise_for_status()
    path = OUT_DIR / f"doubao_{time.strftime('%Y%m%d_%H%M%S')}{suffix}"
    path.write_bytes(r.content)
    return str(path)


@mcp.tool()
def doubao_ping() -> str:
    """检查豆包服务是否在线、是否处于 IP 风控中。生成任何内容前建议先调用确认链路通畅。"""
    try:
        with _client(8) as c:
            r = c.get(f"{API}/v1/models")
        if r.status_code != 200:
            return "⚪ 服务未响应，请在终端运行 ~/doubao2api/start_server.sh"
    except Exception as e:
        return f"⚪ 服务未启动 —— 请在终端运行 ~/doubao2api/start_server.sh（{str(e)[:100]}）"

    try:
        with _client(120) as c:
            r = c.post(f"{API}/v1/chat/completions", json={
                "model": "doubao", "max_tokens": 5, "stream": False,
                "messages": [{"role": "user", "content": "说：好"}],
            })
        if r.status_code == 200:
            return "✅ 豆包链路正常，可生图 / 生视频。"
        return _fail(r.text)
    except Exception as e:
        return f"⚠️ 服务在但请求异常：{str(e)[:200]}"


@mcp.tool()
def doubao_chat(prompt: str, model: str = "doubao") -> str:
    """用豆包做文本工作：写脚本、改稿、审片、起标题、整理资料、头脑风暴。
    model 选填：doubao（快）/ doubao-think（深度思考）/ doubao-expert（专家模式）。"""
    try:
        with _client(300) as c:
            r = c.post(f"{API}/v1/chat/completions", json={
                "model": model, "stream": False,
                "messages": [{"role": "user", "content": prompt}],
            })
        if r.status_code != 200:
            return _fail(r.text)
        return r.json()["choices"][0]["message"].get("content", "")
    except Exception as e:
        return f"❌ 请求异常：{str(e)[:200]}"


@mcp.tool()
def doubao_image(prompt: str, image_path: str = "", ratio: str = "") -> str:
    """文生图或图生图。传入 image_path 即为图生图（只改指定元素时用）。
    ratio 选填：1:1 / 3:4 / 4:3 / 16:9 / 9:16，留空由模型自适应。
    返回：本地保存的图片绝对路径。"""
    try:
        with _client(300) as c:
            if image_path:
                p = Path(image_path).expanduser()
                if not p.exists():
                    return f"❌ 图片不存在：{p}"
                data = {"prompt": prompt}
                if ratio:
                    data["ratio"] = ratio
                r = c.post(f"{API}/v1/images/edits", data=data,
                           files={"image": (p.name, p.read_bytes())})
            else:
                body = {"prompt": prompt}
                if ratio:
                    body["ratio"] = ratio
                r = c.post(f"{API}/v1/images/generations", json=body)
        if r.status_code != 200:
            return _fail(r.text)
        url = r.json()["data"][0]["url"]
    except Exception as e:
        return f"❌ 请求异常：{str(e)[:200]}"
    return f"✅ 图片已保存：{_save(url, '.png')}\n（注：豆包服务端会给图片加水印，无法去除）"


@mcp.tool()
def doubao_video(prompt: str, image_path: str = "", ratio: str = "9:16") -> str:
    """文生视频或图生视频（核心能力）。传入 image_path 即为图生视频。
    ratio：9:16（竖屏，适合短视频）/ 16:9 / 1:1。生成通常需 1-3 分钟。
    返回：本地保存的 mp4 绝对路径 + 封面 URL。"""
    try:
        with _client(900) as c:
            if image_path:
                p = Path(image_path).expanduser()
                if not p.exists():
                    return f"❌ 图片不存在：{p}"
                r = c.post(f"{API}/v1/video/generations",
                           data={"prompt": prompt, "ratio": ratio},
                           files={"image": (p.name, p.read_bytes())})
            else:
                r = c.post(f"{API}/v1/video/generations",
                           json={"prompt": prompt, "ratio": ratio})
        if r.status_code != 200:
            return _fail(r.text)
        item = r.json()["data"][0]
    except Exception as e:
        return f"❌ 请求异常：{str(e)[:200]}"

    out = _save(item["video_url"], ".mp4")
    cover = item.get("cover_url", "")
    return f"✅ 视频已保存：{out}" + (f"\n封面：{cover}" if cover else "")


if __name__ == "__main__":
    mcp.run()