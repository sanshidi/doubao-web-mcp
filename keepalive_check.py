# -*- coding: utf-8 -*-
"""豆包账号池 保活 + 失效巡检

对 accounts.json 里每个账号：
  - 用其 cookie 打一个已登录态接口（/passport/account/info/）。
    * 该请求本身是「保活 ping」：若豆包是滑动过期会话，每次已登录请求都会把过期时间往后推。
    * 同时据此判定账号是否仍然有效。
  - 返回 data.user_id_str / screen_name 即视为 VALID；否则 EXPIRED。

退出码：0 = 全部有效；1 = 有账号失效（供自动化脚本分支判断）。

用法：
  ~/doubao-mcp/.venv/bin/python keepalive_check.py            # 巡检全部
  ~/doubao-mcp/.venv/bin/python keepalive_check.py --account ipone   # 只查某个
  ~/doubao-mcp/.venv/bin/python keepalive_check.py --quiet     # 仅输出汇总行（给自动化）
"""
import argparse
import asyncio
import json
import os
import sys

import aiohttp

from profile_cookies import get_fresh_cookies

BASE = "https://www.doubao.com"
POOL = os.path.expanduser("~/.doubao-mcp/accounts.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.doubao.com/chat",
}


def load_accounts():
    with open(POOL, encoding="utf-8") as f:
        return json.load(f)


async def check_one(acc):
    """返回 (name, status, detail)。status ∈ {VALID, EXPIRED, ERROR}。

    优先用 profile_cookies.get_fresh_cookies 取 cookie：
      - 有持久化 profile 的账号 → 真实启动浏览器加载豆包，提取并自动续期会话；
      - 无 profile 的账号 → 回退 accounts.json 静态 cookie（原有行为）。
    ttl=0 强制每次都重新提取（巡检的意义就是刷新/校验）。
    """
    name = acc["name"]
    try:
        cookies = await get_fresh_cookies(acc, ttl=0)
    except Exception as e:  # noqa: BLE001
        return name, "ERROR", f"get_fresh_cookies: {e}"
    if not cookies.get("sessionid"):
        return name, "EXPIRED", "无 sessionid"
    try:
        async with aiohttp.ClientSession(cookies=cookies, headers=HEADERS) as s:
            async with s.get(f"{BASE}/passport/account/info/", timeout=20) as r:
                text = await r.text()
        try:
            data = json.loads(text).get("data") or {}
        except Exception:
            return name, "ERROR", f"非 JSON 响应: {text[:120]}"
        uid = data.get("user_id_str") or data.get("user_id")
        if uid:
            return name, "VALID", f"user_id={uid} name={data.get('screen_name','')}"
        # data 为空 / 未登录：豆包通常返回 {"data":null,"msg":...} 或 code 非 0
        return name, "EXPIRED", text[:160]
    except Exception as e:  # noqa: BLE001
        return name, "ERROR", f"{type(e).__name__}: {e}"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="", help="只检查指定账号名")
    ap.add_argument("--quiet", action="store_true", help="仅打印汇总行")
    args = ap.parse_args()

    data = load_accounts()
    accounts = data["accounts"]
    if args.account:
        accounts = [a for a in accounts if a["name"] == args.account]
        if not accounts:
            print(f"未找到账号：{args.account}", file=sys.stderr)
            return 2

    results = []
    for acc in accounts:
        res = await check_one(acc)
        results.append(res)
        if not args.quiet:
            name, status, detail = res
            mark = {"VALID": "✅", "EXPIRED": "⚠️", "ERROR": "❌"}[status]
            print(f"{mark} {name:<16} {status:<8} {detail}")

    valid = sum(1 for _, s, _ in results if s == "VALID")
    expired = [n for n, s, _ in results if s == "EXPIRED"]
    errored = [n for n, s, _ in results if s == "ERROR"]
    print(f"\n[汇总] 共 {len(results)} 个 | 有效 {valid} | 失效 {len(expired)} | 异常 {len(errored)}")
    if expired:
        print(f"[需重登] {', '.join(expired)}")
        print("  重新出码：~/doubao-mcp/.venv/bin/python ~/doubao-mcp/login_qr.py "
              f"--name <账号名> --qr-out /Users/edy/WorkBuddy/2026-10-28-22-56-16/doubao_login_qr.png --timeout 600")
    if errored:
        print(f"[异常待查] {', '.join(errored)}")
    return 1 if (expired or errored) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
