"""超大团队（8 万个地址以上）的结构图数据：在 GitHub Actions 里从 Anubis 链读出整个团队、每个人的算力和大节点的官方等级，
写成压缩二进制文件，放到 monita 仓库的 trees 分支，ailgns.dev 通过 /api/bigtree 读取。

文件格式（gzip 压缩后的 trees/<root>.bin.gz）：
  "AILT" 魔数 | uint32 版本 1 | uint32 N（含自己）| float64 生成时间（毫秒）
  N × 20 字节 地址（第 0 个是自己）| N × int32 上级下标（自己为 -1）| N × float32 个人已登记算力 | N × uint8 官方等级
全部小端序。上级一定排在下级前面（按层读出来的顺序）。

    python big_team_build.py 0xROOT [0xROOT2 ...]
"""
import gzip
import json
import os
import struct
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

RPCS = ["https://rpc.anubispace.org", "https://rpc2.anubispace.org", "https://rpc3.anubispace.org"]
COMMUNITY = "0xc9f9a8a2dBf8188AD6610e0239105CAbE67c4993"
POWER = "0x9E68F61E2615cc1D2C4a2D0026bFc1EBFdf68C1b"
REFERRALS = "0xc01b35a1"  # referrals(address,uint256 offset,uint256 limit)
POWER_OF = "0x4f343040"  # totalPowerOf(address)
BATCH = 400
WORKERS = 8
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trees")

_rr = [0]
_lock = threading.Lock()


def pad(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def u256(v):
    return f"{int(v):064x}"


def batch(calls):
    """一批 eth_call（≤BATCH 个），失败换节点重试；返回结果列表（失败的为 None）"""
    body = json.dumps([{"jsonrpc": "2.0", "id": i, "method": "eth_call", "params": [c, "latest"]} for i, c in enumerate(calls)]).encode()
    for attempt in range(8):
        with _lock:
            url = RPCS[_rr[0] % len(RPCS)]
            _rr[0] += 1
        try:
            req = urllib.request.Request(url, body, {"Content-Type": "application/json", "User-Agent": "ailgns-big-tree"})
            arr = json.load(urllib.request.urlopen(req, timeout=90))
            out = [None] * len(calls)
            for x in arr:
                if "result" in x:
                    out[x["id"]] = x["result"]
            return out
        except Exception:
            time.sleep(1 + attempt)
    raise RuntimeError("RPC 一直失败")


def run_batches(calls):
    chunks = [calls[i:i + BATCH] for i in range(0, len(calls), BATCH)]
    with ThreadPoolExecutor(WORKERS) as ex:
        parts = list(ex.map(batch, chunks))
    return [r for p in parts for r in p]


def ref_call(a, off, lim):
    return {"to": COMMUNITY, "data": REFERRALS + pad(a) + u256(off) + u256(lim)}


def build_team(root):
    """按层读出团队：返回 (地址列表, 上级下标列表)，上级排在下级前面"""
    addrs, parents, index = [root], [-1], {root: 0}
    jobs = [(0, 0, 1, True)]  # (上级下标, offset, limit, 是不是试探)
    rnd, t0 = 0, time.time()
    while jobs:
        rnd += 1
        res = run_batches([ref_call(addrs[j[0]], j[1], j[2]) for j in jobs])
        nxt = []
        for j, h in zip(jobs, res):
            if not h or len(h) < 194:
                continue  # 没有直推时 limit=1 会被拒绝
            h = h[2:]
            if j[3]:
                total = int(h[:64], 16)
                for off in range(1, total, 100):
                    nxt.append((j[0], off, min(100, total - off), False))
            n = int(h[128:192], 16)
            for k in range(n):
                c = "0x" + h[(3 + k) * 64 + 24:(4 + k) * 64]
                if c in index:
                    continue
                index[c] = len(addrs)
                addrs.append(c)
                parents.append(j[0])
                nxt.append((index[c], 0, 1, True))
        jobs = nxt
        print(f"  第 {rnd} 轮：已读到 {len(addrs) - 1} 个地址，下一轮 {len(jobs)} 个请求（{time.time() - t0:.0f} 秒）", flush=True)
    return addrs, parents


def read_powers(addrs):
    res = run_batches([{"to": POWER, "data": POWER_OF + pad(a)} for a in addrs])
    return [int(h[2:66], 16) / 1e18 if h and len(h) >= 66 else 0.0 for h in res]


def read_levels(addrs, parents, powers):
    """只给团队算力 ≥ 30 万的人查官方等级（P1 至少三个区各 5 万），最多 400 个"""
    sub = [0.0] * len(addrs)
    for i in range(len(addrs) - 1, 0, -1):
        sub[parents[i]] += sub[i] + powers[i]
    big = sorted([i for i in range(len(addrs)) if sub[i] >= 300000], key=lambda i: -sub[i])[:400]
    levels = [0] * len(addrs)

    def one(i):
        for _ in range(3):
            try:
                r = urllib.request.urlopen(urllib.request.Request("https://awakehelix.io/api/v1/level/" + addrs[i], headers={"User-Agent": "Mozilla/5.0"}), timeout=30)
                levels[i] = int(json.load(r).get("level") or 0)
                return
            except Exception:
                time.sleep(1)
    with ThreadPoolExecutor(10) as ex:
        list(ex.map(one, big))
    return levels


def write(root, addrs, parents, powers, levels):
    n = len(addrs)
    buf = bytearray(b"AILT") + struct.pack("<IId", 1, n, time.time() * 1000)
    for a in addrs:
        buf += bytes.fromhex(a[2:])
    buf += struct.pack("<%di" % n, *parents)
    buf += struct.pack("<%df" % n, *powers)
    buf += bytes(min(255, l) for l in levels)
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, root + ".bin.gz")
    with gzip.open(path, "wb", compresslevel=9) as f:
        f.write(buf)
    return path, len(buf), os.path.getsize(path)


def main():
    for root in [a.lower() for a in sys.argv[1:]]:
        t0 = time.time()
        print(f"{root}：读团队…", flush=True)
        addrs, parents = build_team(root)
        print(f"{root}：{len(addrs) - 1} 个地址，读算力…", flush=True)
        powers = read_powers(addrs)
        print(f"{root}：读官方等级…", flush=True)
        levels = read_levels(addrs, parents, powers)
        path, raw, gz = write(root, addrs, parents, powers, levels)
        print(f"{root}：完成，{len(addrs) - 1} 个地址，{raw / 1e6:.1f} MB → 压缩后 {gz / 1e6:.1f} MB，用时 {time.time() - t0:.0f} 秒", flush=True)


if __name__ == "__main__":
    main()
