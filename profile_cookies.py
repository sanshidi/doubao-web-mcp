# -*- coding: utf-8 -*-
"""持久化浏览器 profile 的 cookie 提取（C 方案·治本）

思路：
  账号的登录态不再只靠 accounts.json 里「手动抽出来的静态 cookie dict」，
  而是优先从一个真实 Chromium 的 user_data_dir 里提取——浏览器自己维护会话、
  自动续期（Doubao 网页端加载时会做会话保活），且会话落在磁盘上，MCP 重启不丢。

  - 若 ~/.doubao-mcp/profiles/<账号名>/ 存在 → 起一个带该 user_data_dir 的无头
    Chromium 加载豆包，从浏览器真实 cookie jar 提取最新 cookie，校验有效后返回。
  - 目录不存在 / 提取失败 / 校验不过 → 回退到 accounts.json 的静态 cookie（现有行为）。
  - 带 TTL 缓存 + 按账号锁，避免每次请求都起浏览器、避免并发重复启动。

用法（在 direct_server.py 等调用方）：
  from profile_cookies import get_fresh_cookies
  cookies = await get_fresh_cookies(acc)   # acc 是 {"name":..., "cookies":{...}}
"""
import asyncio
import json
import os
import time

import aiohttp

PROFILE_ROOT = os.path.expanduser("~/.doubao-mcp/profiles")
DEFAULT_TTL = 300  # 秒：同一账号两次提取之间至少间隔这么久

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.doubao.com/chat",
}
_IDENTITY = "https://www.doubao.com/passport/account/info/"

_cache = {}                       # name -> (ts, cookies_dict)
_locks = {}                       # name -> asyncio.Lock
_global_sem = asyncio.Semaphore(3)  # 限制同时启动的浏览器数量


def profile_dir_for(name: str) -> str:
    """约定：账号 <name> 的持久化 profile 目录。"""
    return os.path.join(PROFILE_ROOT, name)


def _lock_for(name: str) -> asyncio.Lock:
    if name not in _locks:
        _locks[name] = asyncio.Lock()
    return _locks[name]


async def get_fresh_cookies(acc: dict, ttl: int = DEFAULT_TTL) -> dict:
    """返回该账号当前可用的 cookie dict。

    优先 profile（浏览器维护的真实会话），回退静态 cookie。永不直接抛异常，
    失败时返回静态 cookie，保证调用方原有行为不被破坏。
    """
    name = acc.get("name", "")
    # 1) 缓存命中
    if name in _cache and (time.time() - _cache[name][0]) < ttl:
        return _cache[name][1]

    pd = profile_dir_for(name)
    if os.path.isdir(pd):
        async with _lock_for(name):
            # 双重检查，避免同账号并发重复启动浏览器
            if name in _cache and (time.time() - _cache[name][0]) < ttl:
                return _cache[name][1]
            cookies = await _extract_from_profile(pd)
            if cookies:
                _cache[name] = (time.time(), cookies)
                return cookies
            # profile 提取失败/过期：清掉该账号缓存，走静态回退
            _cache.pop(name, None)
            print(f"[profile] 账号「{name}」profile 提取无效，回退静态 cookie")

    # 2) 回退静态 cookie（现有行为）
    return acc.get("cookies", {})


async def _extract_from_profile(pd: str):
    """从持久化 profile 起无头 Chromium，提取并校验 cookie。失败返回 None。"""
    try:
        from playwright.async_api import async_playwright
    except Exception as e:  # noqa: BLE001
        print(f"[profile] 无法导入 playwright，跳过 profile：{e}")
        return None

    try:
        async with _global_sem:
            async with async_playwright() as p:
                ctx = await p.chromium.launch_persistent_context(
                    pd, headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                page = await ctx.new_page()
                try:
                    await page.goto(
                        "https://www.doubao.com/chat/",
                        wait_until="domcontentloaded", timeout=30000,
                    )
                    await page.wait_for_timeout(3000)
                    raw = await ctx.cookies()
                finally:
                    await ctx.close()
    except Exception as e:  # noqa: BLE001
        print(f"[profile] 启动/读取 profile 浏览器失败：{e}")
        return None

    cookies = {c["name"]: c["value"] for c in raw}
    if not cookies.get("sessionid"):
        return None

    # 校验：用这些 cookie 打已登录接口，确认仍是有效会话
    try:
        async with aiohttp.ClientSession(cookies=cookies, headers=_HEADERS) as s:
            async with s.get(_IDENTITY, timeout=20) as r:
                d = await r.json()
        data = (d or {}).get("data") or {}
        if data.get("user_id_str") or data.get("user_id"):
            return cookies
    except Exception:  # noqa: BLE001
        pass
    return None


def invalidate(name: str = ""):
    """手动清缓存（例如检测到某账号调用失败后，强制下次重新提取）。"""
    if name:
        _cache.pop(name, None)
    else:
        _cache.clear()
