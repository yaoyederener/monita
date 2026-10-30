"""AiLGNS 每日投资记录：按北京时间每天一个文件，供 ailgns.dev「团队新增业绩」直接读取，不用在浏览器里逐段查链。

data/deposits/YYYY-MM-DD.json：
  {"date", "from", "to", "final", "rows": [[地址(去掉0x), 方式(0=DAI 买入,1=销毁 LGNS), 投入数量, 新增算力, 区块]]}
保留最近 8 天；已结束的日子只算一次，当天每次运行都重新统计到最新区块。
    python ailgns_deposit_index.py
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from ailgns_lp_monitor import BJT, block_at, block_ts, rpc

POWER = "0x9E68F61E2615cc1D2C4a2D0026bFc1EBFdf68C1b"
DAI_QUEUED = "0x41fd86243836b698405554ae311d223dec7e355200f05fd658fb3adffe567668"
LGNS_BURN = "0x16b309670cfc6e855d1def7ee55e395827e8a8694f4c701ffe0adbc7b75121cc"
DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "deposits")
KEEP_DAYS = 8


def logs(start, end):
    ranges = [(b, min(end, b + 4999)) for b in range(start, end + 1, 5000)]
    with ThreadPoolExecutor(max_workers=6) as ex:
        parts = ex.map(lambda r: rpc("eth_getLogs", [{"address": POWER, "topics": [[DAI_QUEUED, LGNS_BURN]],
                                                     "fromBlock": hex(r[0]), "toBlock": hex(r[1])}]), ranges)
    rows = []
    for part in parts:
        for l in part:
            d = l["data"][2:]
            burn = l["topics"][0] == LGNS_BURN
            amount = int(d[:64], 16) / (1e9 if burn else 1e18)
            rows.append([l["topics"][1][26:], 1 if burn else 0, round(amount, 6), round(int(d[64:128], 16) / 1e18, 4), int(l["blockNumber"], 16)])
    rows.sort(key=lambda r: r[4])
    return rows


def main():
    t0 = time.time()
    os.makedirs(DIR, exist_ok=True)
    latest = int(rpc("eth_blockNumber", []), 16)
    latest_ts = block_ts(latest)
    today = datetime.fromtimestamp(latest_ts, BJT).date()
    keep = set()
    for i in range(KEEP_DAYS - 1, -1, -1):
        day = today - timedelta(days=i)
        key = day.isoformat()
        keep.add(key + ".json")
        path = os.path.join(DIR, key + ".json")
        if os.path.exists(path) and json.load(open(path)).get("final"):
            continue
        start_ts = int(datetime(day.year, day.month, day.day, tzinfo=BJT).timestamp())
        final = day < today
        start = block_at(start_ts, latest, latest_ts)
        end = block_at(start_ts + 86400, latest, latest_ts) - 1 if final else latest
        rows = logs(start, end)
        with open(path, "w") as f:
            json.dump({"date": key, "from": start, "to": end, "final": final, "rows": rows}, f, separators=(",", ":"))
        print(f"{key}: {len(rows)} 笔（区块 {start}-{end}）", file=sys.stderr)
    for name in os.listdir(DIR):  # 只留最近 8 天
        if name.endswith(".json") and name not in keep:
            os.remove(os.path.join(DIR, name))
    print(f"用时 {time.time() - t0:.0f} 秒", file=sys.stderr)


if __name__ == "__main__":
    main()
