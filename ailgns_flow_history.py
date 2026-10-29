"""AILGNS/DAI 每日买卖与资金流向历史，写入 data/ailgns_flows.json，供 data.juzhang.org 读取。

按北京时间自然日统计。已结束的日子只算一次；当天的数据每次运行都重新统计到最新区块。
    python ailgns_flow_history.py
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from ailgns_lp_monitor import BJT, block_at, block_ts, collect, reserves, rpc

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ailgns_flows.json")
FIRST_DAY = "2026-09-26"  # 交易对上线前的日子会因储备为 0 被跳过


def pool_state(block):
    try:
        ail, dai = reserves(block)
    except Exception:
        return None
    return (ail, dai) if ail > 0 else None


def main():
    t0 = time.time()
    try:
        data = json.load(open(OUT, encoding="utf-8"))
    except FileNotFoundError:
        data = {"days": []}
    done = {d["date"]: d for d in data["days"] if d.get("final")}

    latest = int(rpc("eth_blockNumber", []), 16)
    latest_ts = block_ts(latest)
    today = datetime.fromtimestamp(latest_ts, BJT).date()

    def build(day):
        key = day.isoformat()
        if key in done:
            return done[key]
        start_ts = int(datetime(day.year, day.month, day.day, tzinfo=BJT).timestamp())
        final = day < today
        start = block_at(start_ts, latest, latest_ts)
        end = block_at(start_ts + 86400, latest, latest_ts) - 1 if final else latest
        before, after = pool_state(start - 1), pool_state(end)
        if after is None:
            return None
        before = before or (0.0, 0.0)
        s = collect(start, end)
        print(f"{key}: 买 {s['buy_n']} 笔 / 卖 {s['sell_n']} 笔，池子 {after[1] - before[1]:+,.0f} DAI", file=sys.stderr)
        return {
            "date": key,
            "final": final,
            "buy_n": s["buy_n"], "buy_dai": round(s["buy_dai"], 2), "buy_max": round(s["buy_max"], 2),
            "sell_n": s["sell_n"], "sell_dai": round(s["sell_dai"], 2), "sell_max": round(s["sell_max"], 2),
            "lp_add_dai": round(s["mint_dai"], 2), "lp_remove_dai": round(s["burn_dai"], 2),
            "pool_dai_start": round(before[1], 2), "pool_dai_end": round(after[1], 2),
            "price_end": round(after[1] / after[0], 4),
            "end_block": end,
        }

    first = datetime.strptime(FIRST_DAY, "%Y-%m-%d").date()
    all_days = [first + timedelta(days=i) for i in range((today - first).days + 1)]
    with ThreadPoolExecutor(max_workers=6) as ex:  # 每天互不依赖，并行补历史
        days = [d for d in ex.map(build, all_days) if d]

    data = {
        "pair": "AILGNS/DAI",
        "updated": datetime.fromtimestamp(latest_ts, BJT).isoformat(timespec="seconds"),
        "latest_block": latest,
        "days": days,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(f"写入 {len(days)} 天，用时 {time.time() - t0:.0f} 秒", file=sys.stderr)


if __name__ == "__main__":
    main()
