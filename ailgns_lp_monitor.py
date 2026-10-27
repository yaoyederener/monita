"""AILGNS/DAI 流动性池监控：统计过去 N 小时的买单、卖单和资金流向，发送到 Telegram 群。

只用 Python 标准库，可以直接在 GitHub Actions 或本地运行：
    python ailgns_lp_monitor.py            # 统计并发送到 Telegram
    python ailgns_lp_monitor.py --dry-run  # 只打印，不发送

需要的环境变量（发送时）：BOT_TOKEN、CHAT_ID
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

RPCS = [
    "https://rpc.anubispace.org",
    "https://rpc2.anubispace.org",
    "https://rpc3.anubispace.org",
    "https://rpc4.anubispace.org",
]
PAIR = "0x0a1Cce65d995C6C5100e6F5bA3c388Ef34CEcaf0"  # AILGNS/DAI，token0=AILGNS，token1=DAI
EXPLORER = "https://anubisscan.io"
MAX_RANGE = 5000  # 节点单次 eth_getLogs 最多 5000 个块

TOPIC_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
TOPIC_MINT = "0x4c209b5fc8ad50758f13e2e1088ba56a560dff690a1c6fef26394f4c03821c4f"
TOPIC_BURN = "0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4a789976e6d81936496"

BJT = timezone(timedelta(hours=8))
WEI = 10**18


def rpc(method, params):
    last_err = None
    for attempt in range(6):
        url = RPCS[attempt % len(RPCS)]
        try:
            req = urllib.request.Request(
                url,
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                {"Content-Type": "application/json", "User-Agent": "ailgns-monitor"},
            )
            r = json.load(urllib.request.urlopen(req, timeout=60))
            if "error" in r:
                raise RuntimeError(r["error"])
            return r["result"]
        except Exception as e:  # 换一个节点重试
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"RPC {method} 失败: {last_err}")


def block_ts(n):
    return int(rpc("eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)


def block_at(ts, latest, latest_ts):
    """找到时间戳 >= ts 的第一个块（先按平均出块时间估算，再二分修正）。"""
    sample = max(1, latest - 10000)
    bt = (latest_ts - block_ts(sample)) / (latest - sample)
    lo = max(0, int(latest - (latest_ts - ts) / bt) - 3000)
    hi = min(latest, int(latest - (latest_ts - ts) / bt) + 3000)
    while block_ts(lo) > ts and lo > 0:
        lo = max(0, lo - 20000)
    while block_ts(hi) < ts and hi < latest:
        hi = min(latest, hi + 20000)
    while lo < hi:
        mid = (lo + hi) // 2
        if block_ts(mid) < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def reserves(block):
    r = rpc("eth_call", [{"to": PAIR, "data": "0x0902f1ac"}, hex(block)])[2:]
    return int(r[:64], 16) / WEI, int(r[64:128], 16) / WEI


def words(data):
    d = data[2:]
    return [int(d[i:i + 64], 16) for i in range(0, len(d), 64)]


def collect(start, end):
    s = {
        "buy_n": 0, "buy_dai": 0.0, "buy_ail": 0.0, "buy_max": 0.0,
        "sell_n": 0, "sell_dai": 0.0, "sell_ail": 0.0, "sell_max": 0.0,
        "mint_n": 0, "mint_dai": 0.0, "mint_ail": 0.0,
        "burn_n": 0, "burn_dai": 0.0, "burn_ail": 0.0,
    }
    for a in range(start, end + 1, MAX_RANGE):
        b = min(a + MAX_RANGE - 1, end)
        logs = rpc("eth_getLogs", [{
            "address": PAIR, "fromBlock": hex(a), "toBlock": hex(b),
            "topics": [[TOPIC_SWAP, TOPIC_MINT, TOPIC_BURN]],
        }])
        for log in logs:
            t, w = log["topics"][0], words(log["data"])
            if t == TOPIC_SWAP:
                ail_in, dai_in, ail_out, dai_out = (x / WEI for x in w[:4])
                net_dai = dai_in - dai_out
                if net_dai > 0:  # 付出 DAI、拿走 AILGNS = 买
                    s["buy_n"] += 1
                    s["buy_dai"] += net_dai
                    s["buy_ail"] += ail_out - ail_in
                    s["buy_max"] = max(s["buy_max"], net_dai)
                elif net_dai < 0:  # 付出 AILGNS、拿走 DAI = 卖
                    s["sell_n"] += 1
                    s["sell_dai"] += -net_dai
                    s["sell_ail"] += ail_in - ail_out
                    s["sell_max"] = max(s["sell_max"], -net_dai)
            elif t == TOPIC_MINT:
                s["mint_n"] += 1
                s["mint_ail"] += w[0] / WEI
                s["mint_dai"] += w[1] / WEI
            elif t == TOPIC_BURN:
                s["burn_n"] += 1
                s["burn_ail"] += w[0] / WEI
                s["burn_dai"] += w[1] / WEI
    return s


def fmt(x, nd=0):
    return f"{x:,.{nd}f}"


def signed(x, nd=0):
    return ("+" if x >= 0 else "-") + fmt(abs(x), nd)


def pct(new, old):
    return (new - old) / old * 100 if old else 0.0


def build_report(hours):
    latest = int(rpc("eth_blockNumber", []), 16)
    latest_ts = block_ts(latest)
    start = block_at(latest_ts - hours * 3600, latest, latest_ts)
    s = collect(start, latest)
    ail0, dai0 = reserves(start - 1)
    ail1, dai1 = reserves(latest)
    p0, p1 = dai0 / ail0, dai1 / ail1

    pool_net = dai1 - dai0
    end_time = datetime.fromtimestamp(latest_ts, BJT).strftime("%Y-%m-%d %H:%M")

    if pool_net > 0:
        flow = f"🟢 <b>LP 净流入：{signed(pool_net)} DAI</b>"
    elif pool_net < 0:
        flow = f"🔴 <b>LP 净流出：{signed(pool_net)} DAI</b>"
    else:
        flow = "⚪ <b>LP 持平：0 DAI</b>"

    lines = [
        "📊 <b>AILGNS 流动性监控（AILGNS/DAI）</b>",
        f"统计区间：过去 {hours} 小时（截至北京时间 {end_time}）",
        "",
        f"🟢 <b>买单</b>：{fmt(s['buy_n'])} 笔｜{fmt(s['buy_dai'])} DAI",
        f"　　买入 {fmt(s['buy_ail'], 2)} AILGNS｜最大单笔 {fmt(s['buy_max'])} DAI",
        f"🔴 <b>卖单</b>：{fmt(s['sell_n'])} 笔｜{fmt(s['sell_dai'])} DAI",
        f"　　卖出 {fmt(s['sell_ail'], 2)} AILGNS｜最大单笔 {fmt(s['sell_max'])} DAI",
        "",
        f"{flow}（{signed(pct(dai1, dai0), 2)}%）",
        f"　　当前储备：{fmt(dai1)} DAI / {fmt(ail1, 2)} AILGNS",
        f"💲 价格：{fmt(p1, 2)} DAI（{hours} 小时 {signed(pct(p1, p0), 2)}%）",
        "",
        f'<a href="{EXPLORER}/address/{PAIR}">在浏览器查看交易对</a>',
    ]
    return "\n".join(lines)


def send_telegram(text):
    token = os.environ["BOT_TOKEN"]
    chat_id = os.environ["CHAT_ID"]
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    r = json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30))
    if not r.get("ok"):
        raise RuntimeError(f"Telegram 发送失败: {r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    report = build_report(args.hours)
    print(report)
    print(f"\n(用时 {time.time() - t0:.0f} 秒)", file=sys.stderr)
    if not args.dry_run:
        send_telegram(report)
        print("已发送到 Telegram", file=sys.stderr)


if __name__ == "__main__":
    main()
