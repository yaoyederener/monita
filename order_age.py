"""🕐 让客户先吃：机器人（套利 / 撮合）只碰「挂出超过 YIELD 秒还没人要」的单（站长 2026-11-08 定）。
合约里没有挂单时间，所以用机器人第一次看到这张单的时间当作挂出时间，记在 data/order_seen.json（每轮 5 分钟看一次，误差可接受）。
    seen = note(otc, book)      # 记下这轮看到的单，返回 {id: 首次看到的时间}
    fresh(seen, oid, now)       # True = 这张单太新，机器人先让客户
"""
import json
import os
import time

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "order_seen.json")
YIELD = 120  # 秒


def _load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def note(otc, book, now=None):
    now = now or time.time()
    st = _load()
    k = otc.lower()
    seen = st.get(k) or {}
    ids = {str(o[0]) for o in book if o[3] and o[7] > 0}
    seen = {i: t for i, t in seen.items() if i in ids}  # 没了的单不留
    for i in ids:
        seen.setdefault(i, now)
    st[k] = seen
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as f:
            json.dump(st, f)
    except Exception:
        pass
    return seen


def fresh(seen, oid, now=None):
    return (now or time.time()) - seen.get(str(oid), 0) < YIELD
