"""AiLGNS 每日投资记录：按北京时间每天一个文件，供 ailgns.dev「团队新增业绩」直接读取，不用在浏览器里逐段查链。

data/deposits/YYYY-MM-DD.json：
  {"date", "from", "to", "final", "v": 2, "rows": [[地址(去掉0x), 方式(0=DAI 买入,1=销毁 LGNS), 投入数量, 新增算力, 区块]],
   "rows2": [[地址(去掉0x), 销毁的 LGNS, 按市价折的 DAI, 区块]]}
rows2（2026-11-18 加，客户反馈「销毁 LGNS 统计得太少」）：质押合约里按市价销毁 LGNS 的记录，一天约 100 笔、七八十万枚，以前完全没读。
  链上核过：这一种销毁之后官方算力合约里的已登记算力不增加，所以单独放一个键，不混进 rows（老版本网页不认识它，放进 rows 会被当成 DAI 买入）。
  v=2 表示这个文件带 rows2；已结束但没有 v=2 的旧文件会重算一次。
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
STAKE = "0x0664f1369209677fecb279f5ec3df1e53a22212e"  # 质押合约
LGNS_BURN2 = "0x13b44a18f3f1ffeb7b3e65ecd3210084fae31ad0ed6a2a9ce02f67427deb06f5"  # topics[2]=地址，data=[按市价折的 DAI, LGNS(9 位), DAI×2.5]
VERSION = 2
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


def logs2(start, end):
    ranges = [(b, min(end, b + 4999)) for b in range(start, end + 1, 5000)]
    with ThreadPoolExecutor(max_workers=6) as ex:
        parts = ex.map(lambda r: rpc("eth_getLogs", [{"address": STAKE, "topics": [LGNS_BURN2],
                                                     "fromBlock": hex(r[0]), "toBlock": hex(r[1])}]), ranges)
    rows = []
    for part in parts:
        for l in part:
            d = l["data"][2:]
            rows.append([l["topics"][2][26:], round(int(d[64:128], 16) / 1e9, 6), round(int(d[:64], 16) / 1e18, 4), int(l["blockNumber"], 16)])
    rows.sort(key=lambda r: r[3])
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
        if os.path.exists(path):
            old = json.load(open(path))
            if old.get("final") and old.get("v", 1) >= VERSION:
                continue
        start_ts = int(datetime(day.year, day.month, day.day, tzinfo=BJT).timestamp())
        final = day < today
        start = block_at(start_ts, latest, latest_ts)
        end = block_at(start_ts + 86400, latest, latest_ts) - 1 if final else latest
        rows = logs(start, end)
        rows2 = logs2(start, end)
        with open(path, "w") as f:
            json.dump({"date": key, "from": start, "to": end, "final": final, "v": VERSION, "rows": rows, "rows2": rows2}, f, separators=(",", ":"))
        print(f"{key}: {len(rows)} 笔 + 质押合约销毁 {len(rows2)} 笔（区块 {start}-{end}）", file=sys.stderr)
    for name in os.listdir(DIR):  # 只留最近 8 天
        if name.endswith(".json") and name not in keep:
            os.remove(os.path.join(DIR, name))
    print(f"用时 {time.time() - t0:.0f} 秒", file=sys.stderr)


if __name__ == "__main__":
    main()
