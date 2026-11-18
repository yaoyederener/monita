"""AiLGNS 每日投资记录：按北京时间每天一个文件，供 ailgns.dev「团队新增业绩」直接读取，不用在浏览器里逐段查链。

data/deposits/YYYY-MM-DD.json：
  {"date", "from", "to", "final", "v": 2, "rows": [[地址(去掉0x), 方式(0=DAI 买入,1=销毁 LGNS), 投入数量, 新增算力, 区块]],
   "rows2": [[地址(去掉0x), 销毁的 LGNS, 按市价折的 DAI, 区块]]}
rows2（2026-11-18 加，客户反馈「销毁 LGNS 统计得太少」）：质押合约里按市价销毁 LGNS 的记录，一天约 100 笔、七八十万枚，以前完全没读。
  链上核过：这一种销毁之后官方算力合约里的已登记算力不增加，所以单独放一个键，不混进 rows（老版本网页不认识它，放进 rows 会被当成 DAI 买入）。
  v=2 表示这个文件带 rows2；已结束但没有 v=2 的旧文件会重算一次。
fails（2026-11-19 加，站长要「把点了销毁但没抢到的也显示出来」）：[[地址(去掉0x), 想销毁的 LGNS, 区块]]。
  算力合约里的销毁每天中午 11:59 开放、全网 400 个名额、一两秒抢完；没抢到的交易失败，不留事件，只能逐块读交易找出来。
  只看当天第一笔成功销毁前 30 个块到后 300 个块（约 5 分钟）；当天后来抢到了的地址不算。每个文件只扫一次（failsDone），每次运行最多补 2 天，免得超时。
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
BURN_SEL = "0xffa6b88a"  # 算力合约「销毁 LGNS 换算力」的方法，参数 = 想销毁的数量（9 位小数）
WIN_BEFORE, WIN_AFTER, FAILS_PER_RUN = 30, 300, 2
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


def failed_attempts(first, ok_addrs):
    """中午那一阵点了销毁、但当天没成功的地址：[[地址, 想销毁的 LGNS, 区块]]"""
    def blk(n):
        return rpc("eth_getBlockByNumber", [hex(n), True])
    with ThreadPoolExecutor(max_workers=8) as ex:
        blocks = list(ex.map(blk, range(first - WIN_BEFORE, first + WIN_AFTER + 1)))
    want = {}
    for b in blocks:
        for t in (b or {}).get("transactions", []):
            if (t.get("to") or "").lower() != POWER.lower() or t.get("input", "")[:10] != BURN_SEL:
                continue
            a = t["from"][2:].lower()
            if a in ok_addrs:
                continue
            amt = int(t["input"][10:74], 16) / 1e9 if len(t["input"]) >= 74 else 0
            if a not in want or amt > want[a][0]:
                want[a] = [round(amt, 6), int(b["number"], 16)]
    return sorted([[a, v[0], v[1]] for a, v in want.items()], key=lambda r: r[2])


def fill_fails(latest):
    done = 0
    for name in sorted(os.listdir(DIR), reverse=True):  # 新的日子先补
        if not name.endswith(".json") or done >= FAILS_PER_RUN:
            continue
        path = os.path.join(DIR, name)
        d = json.load(open(path))
        if d.get("failsDone"):
            continue
        burns = [r for r in d["rows"] if r[1] == 1]
        if not burns:
            if d.get("final"):  # 这一天没有中午那批销毁：没什么可扫的
                d["fails"], d["failsDone"] = [], True
                json.dump(d, open(path, "w"), separators=(",", ":"))
            continue
        first = min(r[4] for r in burns)
        if latest < first + WIN_AFTER:
            continue  # 那一阵还没过完，下次再扫
        t = time.time()
        d["fails"] = failed_attempts(first, set(r[0] for r in burns))
        d["failsDone"], d["failsWin"] = True, [first - WIN_BEFORE, first + WIN_AFTER]
        json.dump(d, open(path, "w"), separators=(",", ":"))
        done += 1
        print(f"{name}: 中午没抢到的 {len(d['fails'])} 个地址（用时 {time.time() - t:.0f} 秒）", file=sys.stderr)


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
        old = {}
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
            out = {"date": key, "from": start, "to": end, "final": final, "v": VERSION, "rows": rows, "rows2": rows2}
            for k in ("fails", "failsDone", "failsWin"):  # 已经扫过的「没抢到」名单带过去，不重扫
                if k in old:
                    out[k] = old[k]
            json.dump(out, f, separators=(",", ":"))
        print(f"{key}: {len(rows)} 笔 + 质押合约销毁 {len(rows2)} 笔（区块 {start}-{end}）", file=sys.stderr)
    for name in os.listdir(DIR):  # 只留最近 8 天
        if name.endswith(".json") and name not in keep:
            os.remove(os.path.join(DIR, name))
    fill_fails(latest)
    print(f"用时 {time.time() - t0:.0f} 秒", file=sys.stderr)


if __name__ == "__main__":
    main()
