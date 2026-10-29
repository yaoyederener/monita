"""ETH 价差提醒：Bitget 现货 vs 以太坊链上 Uniswap，价差超过阈值就每分钟提醒一次。

只用 Python 标准库：
    python3 eth_arb_alert.py                 # 默认：每 60 秒检查，价差 >= 20 美元提醒，按 10 枚报价
    python3 eth_arb_alert.py --gap 15 --size 20

提醒方式：
  - Mac 系统通知 + 提示音（默认就有）
  - 电报：在本文件旁边建一个 .env 文件，写上两行 BOT_TOKEN=... 和 CHAT_ID=...（不要发到聊天里）

链上价格是 Uniswap V3 ETH/USDC 0.05% 池对 --size 枚的实际可成交报价（已含手续费和滑点），
Bitget 用 ETH/USDT 的买一、卖一价。只读，不下单。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

BITGET = "https://api.bitget.com/api/v2/spot/market/tickers?symbol=ETHUSDT"
RPCS = ["https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com", "https://rpc.ankr.com/eth"]
QUOTER = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"  # Uniswap V3 QuoterV2
WETH = "c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
FEE = 500  # 0.05% 池
HERE = os.path.dirname(os.path.abspath(__file__))


def load_env():
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def get(url, data=None):
    req = urllib.request.Request(url, data, {"Content-Type": "application/json", "User-Agent": "eth-arb-alert"})
    return json.load(urllib.request.urlopen(req, timeout=15))


def bitget():
    t = get(BITGET)["data"][0]
    return float(t["bidPr"]), float(t["askPr"])


def quote(token_in, token_out, amount_in):
    """QuoterV2.quoteExactInputSingle，返回 amountOut。"""
    data = ("0xc6a5026a" + token_in.rjust(64, "0") + token_out.rjust(64, "0")
            + f"{amount_in:064x}" + f"{FEE:064x}" + "0" * 64)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": QUOTER, "data": data}, "latest"]}).encode()
    last = None
    for url in RPCS:
        try:
            r = get(url, body)
            if "result" in r:
                return int(r["result"][2:66], 16)
            last = r.get("error")
        except Exception as e:  # 换一个节点
            last = e
    raise RuntimeError(f"链上报价失败: {last}")


def check(size):
    bid, ask = bitget()
    # 链上卖 size 枚 ETH 能拿到多少 USDC → 链上卖价
    chain_sell = quote(WETH, USDC, int(size * 1e18)) / 1e6 / size
    # 链上用 size×ask 的 USDC 能买多少 ETH → 链上买价
    usdc_in = int(size * ask * 1e6)
    chain_buy = usdc_in / 1e6 / (quote(USDC, WETH, usdc_in) / 1e18)
    return {
        "bid": bid, "ask": ask, "chain_sell": chain_sell, "chain_buy": chain_buy,
        "gap_a": chain_sell - ask,  # Bitget 买、链上卖
        "gap_b": bid - chain_buy,   # 链上买、Bitget 卖
    }


def notify(title, text):
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification "{text}" with title "{title}" sound name "Glass"'],
                       check=False, capture_output=True)
    token, chat = os.environ.get("BOT_TOKEN"), os.environ.get("CHAT_ID")
    if token and chat:
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": f"{title}\n{text}"}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=15)
        except Exception as e:
            print("  电报发送失败:", e)
    print(f"  已提醒：{title}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap", type=float, default=20, help="价差阈值（美元/枚），默认 20")
    ap.add_argument("--size", type=float, default=10, help="按多少枚报价，默认 10")
    ap.add_argument("--every", type=int, default=60, help="检查间隔秒数，默认 60")
    ap.add_argument("--once", action="store_true", help="只检查一次")
    ap.add_argument("--minutes", type=float, default=0, help="运行多少分钟后退出，0 = 一直运行")
    ap.add_argument("--test", action="store_true", help="先发一条当前价差的测试消息")
    args = ap.parse_args()
    load_env()
    tg = "电报 + Mac 通知" if os.environ.get("BOT_TOKEN") and os.environ.get("CHAT_ID") else "Mac 通知"
    print(f"开始监控：价差 >= ${args.gap:g} 提醒，按 {args.size:g} 枚报价，每 {args.every} 秒检查（{tg}）。Ctrl+C 停止\n")

    if args.test:
        r = check(args.size)
        notify("ETH 价差监控已启动",
               f"Bitget 卖一 {r['ask']:.2f}，链上卖 {r['chain_sell']:.2f}（{args.size:g} 枚实价），当前价差 ${r['gap_a']:+.2f}。"
               f"价差 >= ${args.gap:g} 时每分钟提醒一次")

    stop_at = time.time() + args.minutes * 60 if args.minutes else None
    while True:
        now = datetime.now().strftime("%H:%M:%S")
        try:
            r = check(args.size)
            best = max(r["gap_a"], r["gap_b"])
            print(f"{now}  Bitget 买一 {r['bid']:.2f} / 卖一 {r['ask']:.2f}  链上卖 {r['chain_sell']:.2f} / 买 {r['chain_buy']:.2f}  "
                  f"价差 {r['gap_a']:+.2f} / {r['gap_b']:+.2f}" + ("  ← 提醒" if best >= args.gap else ""), flush=True)
            if r["gap_a"] >= args.gap:
                notify(f"ETH 价差 ${r['gap_a']:.1f}",
                       f"Bitget 买 {r['ask']:.2f}，链上卖 {r['chain_sell']:.2f}（{args.size:g} 枚实价），"
                       f"毛利约 ${r['gap_a'] * args.size:,.0f}/{args.size:g}枚")
            if r["gap_b"] >= args.gap:
                notify(f"ETH 价差 ${r['gap_b']:.1f}",
                       f"链上买 {r['chain_buy']:.2f}（{args.size:g} 枚实价），Bitget 卖 {r['bid']:.2f}，"
                       f"毛利约 ${r['gap_b'] * args.size:,.0f}/{args.size:g}枚")
        except Exception as e:
            print(f"{now}  读取失败：{e}")
        if args.once or (stop_at and time.time() + args.every > stop_at):
            break
        time.sleep(args.every)


if __name__ == "__main__":
    main()
