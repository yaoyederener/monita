"""ETH 价差提醒：Bitget 现货 vs 以太坊链上 Uniswap，价差超过阈值就每分钟提醒一次。

只用 Python 标准库：
    python3 eth_arb_alert.py                 # 默认：每 60 秒检查，价差 >= 20 美元提醒，按 10 枚报价
    python3 eth_arb_alert.py --gap 15 --size 20

提醒方式：
  - Mac 系统通知 + 提示音（默认就有）
  - 电报：在本文件旁边建一个 .env 文件，写上两行 BOT_TOKEN=... 和 CHAT_ID=...（不要发到聊天里）

链上看以太坊主网 Uniswap V3 的 6 个池子（ETH/USDT、ETH/USDC，各 0.01%/0.05%/0.3%），取最好的那个：
  - 现价：卖 0.01 枚的报价，跟 Uniswap 页面上看到的价格一致，用它判断是否提醒
  - 实价：卖 --size 枚的实际可成交价（含手续费和滑点），写在提醒里供参考
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
STABLES = {"USDT": "dac17f958d2ee523a2206206994597c13d831ec7", "USDC": "a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"}
POOLS = [(name, fee) for name in STABLES for fee in (100, 500, 3000)]  # 都是 6 位小数
SPOT = 0.01  # 用 0.01 枚的报价当作现价
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


def quote(token_in, token_out, amount_in, fee):
    """QuoterV2.quoteExactInputSingle，返回 amountOut；池子不存在或流动性不够时返回 0。"""
    data = ("0xc6a5026a" + token_in.rjust(64, "0") + token_out.rjust(64, "0")
            + f"{amount_in:064x}" + f"{fee:064x}" + "0" * 64)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": QUOTER, "data": data}, "latest"]}).encode()
    last = None
    for url in RPCS:
        try:
            r = get(url, body)
            if "result" in r:
                return int(r["result"][2:66], 16)
            if "revert" in str(r.get("error")):
                return 0
            last = r.get("error")
        except Exception as e:  # 换一个节点
            last = e
    raise RuntimeError(f"链上报价失败: {last}")


def sell_price(name, fee, size):
    """在这个池子卖 size 枚 ETH 的均价。"""
    out = quote(WETH, STABLES[name], int(size * 1e18), fee)
    return out / 1e6 / size if out else 0


def buy_price(name, fee, size, ref):
    """在这个池子花 size×ref 的稳定币买 ETH 的均价。"""
    usd = int(size * ref * 1e6)
    out = quote(STABLES[name], WETH, usd, fee)
    return usd / 1e6 / (out / 1e18) if out else float("inf")


def pool_name(p):
    return f"ETH/{p[0]} {p[1] / 10000:g}%"


def check(size):
    bid, ask = bitget()
    sells = {p: sell_price(*p, SPOT) for p in POOLS}
    buys = {p: buy_price(*p, SPOT, ask) for p in POOLS}
    sp = max(sells, key=sells.get)  # 链上卖得最贵的池子
    bp = min(buys, key=buys.get)    # 链上买得最便宜的池子
    return {
        "bid": bid, "ask": ask,
        "sell_pool": pool_name(sp), "chain_sell": sells[sp], "chain_sell_real": sell_price(*sp, size),
        "buy_pool": pool_name(bp), "chain_buy": buys[bp], "chain_buy_real": buy_price(*bp, size, ask),
        "gap_a": sells[sp] - ask,  # Bitget 买、链上卖
        "gap_b": bid - buys[bp],   # 链上买、Bitget 卖
    }


def notify(title, text):
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification "{text.replace(chr(10), " ")}" with title "{title}" sound name "Glass"'],
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
        notify("ETH 价差监控已更新",
               f"现在同时看 Uniswap ETH/USDT 和 ETH/USDC 共 6 个池子，取最好的价格。\n"
               f"Bitget 卖一 {r['ask']:.2f}，链上 {r['sell_pool']} 现价 {r['chain_sell']:.2f}，当前价差 ${r['gap_a']:+.2f}。\n"
               f"价差 >= ${args.gap:g} 时每分钟提醒一次")

    stop_at = time.time() + args.minutes * 60 if args.minutes else None
    while True:
        now = datetime.now().strftime("%H:%M:%S")
        try:
            r = check(args.size)
            best = max(r["gap_a"], r["gap_b"])
            print(f"{now}  Bitget {r['bid']:.2f}/{r['ask']:.2f}  链上卖 {r['chain_sell']:.2f}（{r['sell_pool']}）"
                  f" 买 {r['chain_buy']:.2f}（{r['buy_pool']}）  价差 {r['gap_a']:+.2f} / {r['gap_b']:+.2f}"
                  + ("  ← 提醒" if best >= args.gap else ""), flush=True)
            if r["gap_a"] >= args.gap:
                notify(f"ETH 价差 ${r['gap_a']:.1f}：Bitget 买、链上卖",
                       f"Bitget 卖一 {r['ask']:.2f}\n链上 {r['sell_pool']} 现价 {r['chain_sell']:.2f}\n"
                       f"卖 {args.size:g} 枚实价 {r['chain_sell_real']:.2f}，毛利约 ${(r['chain_sell_real'] - r['ask']) * args.size:,.0f}")
            if r["gap_b"] >= args.gap:
                notify(f"ETH 价差 ${r['gap_b']:.1f}：链上买、Bitget 卖",
                       f"链上 {r['buy_pool']} 现价 {r['chain_buy']:.2f}\nBitget 买一 {r['bid']:.2f}\n"
                       f"买 {args.size:g} 枚实价 {r['chain_buy_real']:.2f}，毛利约 ${(r['bid'] - r['chain_buy_real']) * args.size:,.0f}")
        except Exception as e:
            print(f"{now}  读取失败：{e}")
        if args.once or (stop_at and time.time() + args.every > stop_at):
            break
        time.sleep(args.every)


if __name__ == "__main__":
    main()
