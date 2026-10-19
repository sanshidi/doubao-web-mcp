# -*- coding: utf-8 -*-
"""端到端验证：修好的 generate_video（memory_type=0 + 会话轮询取回）。"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from doubao_client import DoubaoChatClient  # noqa: E402

POOL = os.path.expanduser("~/.doubao-mcp/accounts.json")


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def load_active():
    d = json.load(open(POOL, encoding="utf-8"))
    return next(a for a in d["accounts"] if a["name"] == d.get("active"))


async def main():
    acc = load_active()
    log(f"account={acc['name']}")

    async with DoubaoChatClient(cookies=acc["cookies"], ms_token="") as c:
        t0 = time.time()
        result = await c.generate_video(
            "一只金毛犬在秋天的公园里奔跑，落叶飞舞，电影感",
            ratio="16:9",
            video_model="seedance_v2.0",
            video_duration=5,
            poll_timeout=420,
        )
        log(f"elapsed={time.time()-t0:.0f}s")
        log(f"conversation_id={result.conversation_id}")
        log(f"agent_reply={result.agent_reply[:200]}")
        log(f"videos={len(result.videos)}")
        for v in result.videos:
            log(f"  url={v.video_url[:110]}")
            log(f"  vid={v.vid} {v.width}x{v.height} dur={v.duration}")
            log(f"  cover={v.cover_url[:80]}")


asyncio.run(main())
