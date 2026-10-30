"""AiLGNS OTC 挂单播报：把托管合约里正在挂的买单、卖单发到 DeBox 群。

有新挂单时马上播报一次；没有新挂单时，每隔 DEBOX_SUMMARY_HOURS 小时（默认 2）播报一次当前挂单汇总。没有挂单就不发。
需要的环境变量（放在 GitHub Secrets）：
  DEBOX_API_KEY     DeBox 机器人的 App Key（BotMother → /mybots → 机器人管理页）
  DEBOX_API_SECRET  DeBox 机器人的 App Secret（可选，用于签名）
  DEBOX_GROUP_ID    要播报的群 gid（群分享链接里 id= 后面那串）
    python otc_debox_broadcast.py           # 按规则判断要不要播报
    python otc_debox_broadcast.py --test    # 立刻发一条当前挂单播报（测试用）
    python otc_debox_broadcast.py --dry-run # 只打印消息，不发送
"""
import argparse
import hashlib
import json
import os
import random
import time
import urllib.request
import uuid

from ailgns_lp_monitor import rpc

OTC = "0x4e730be6d46be71223ded5a32dc45799f6d4bc15"
PAIR = "0x0a1Cce65d995C6C5100e6F5bA3c388Ef34CEcaf0"  # AiLGNS/DAI，token0 = AiLGNS
SEL_COUNT = "0x35daa731"
SEL_GET = "0x8f72fc77"
SITE = "https://ailgns.dev/#/otc"
API = "https://open.debox.pro/openapi/bot/sendMessage"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "otc_broadcast.json")
MAX_LINES = 8  # 买单、卖单各最多列几张

URGE = [
    "⚡ 好价不等人，手慢无！",
    "⚡ 低于市价的单子一出就被抢，手慢无！",
    "⚡ 先到先得，成交即到账，手慢无！",
    "⚡ 挂单随时可能被吃掉，手慢无！",
]


def u256(v):
    return f"{int(v):064x}"


def market_price():
    h = rpc("eth_call", [{"to": PAIR, "data": "0x0902f1ac"}, "latest"])[2:]
    return (int(h[64:128], 16) / 1e18) / (int(h[:64], 16) / 1e18)


def open_orders(now):
    count = int(rpc("eth_call", [{"to": OTC, "data": SEL_COUNT}, "latest"]), 16)
    out = []
    for start in range(max(0, count - 1000), count, 500):
        h = rpc("eth_call", [{"to": OTC, "data": SEL_GET + u256(start) + u256(min(500, count - start))}, "latest"])[2:]
        w = [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]
        for i in range(w[1]):
            maker, sell, is_open, expiry, give, want, left = w[2 + i * 7: 9 + i * 7]
            if not (is_open and expiry > now and left > 0):
                continue
            if sell:  # 卖 AiLGNS 换 DAI
                price, qty = want / give, left / 1e18
            else:  # 拿 DAI 买 AiLGNS
                price, qty = give / want, left / 1e18 * want / give
            out.append({"id": start + i, "sell": bool(sell), "price": price, "qty": qty, "left": expiry - now})
    return count, out


def fmt(x, d=2):
    s = f"{x:,.{d}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def left_text(sec):
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    return f"{d}天{h}小时" if d else f"{h}小时{m}分" if h else f"{max(1, m)}分钟"


def message(orders, mkt, new_ids):
    sells = sorted([o for o in orders if o["sell"]], key=lambda o: o["price"])
    buys = sorted([o for o in orders if not o["sell"]], key=lambda o: -o["price"])
    title = f"🔥 AiLGNS OTC 新挂单 {len(new_ids)} 张！" if new_ids else "📢 AiLGNS OTC 实时挂单"
    lines = [title, f"市价参考：{fmt(mkt)} DAI / AiLGNS", ""]

    def block(name, icon, lst):
        total = sum(o["qty"] for o in lst)
        lines.append(f"{icon} {name} {len(lst)} 张 · 共 {fmt(total, 4)} AiLGNS · 约 {fmt(sum(o['qty'] * o['price'] for o in lst), 0)} DAI")
        for k, o in enumerate(lst[:MAX_LINES], 1):
            diff = (o["price"] / mkt - 1) * 100 if mkt else 0
            tag = f"比市价{'高' if diff >= 0 else '低'} {abs(diff):.1f}%"
            star = "🆕" if o["id"] in new_ids else f"{k}."
            lines.append(f"{star} {fmt(o['qty'], 4)} 枚 × {fmt(o['price'])} DAI = {fmt(o['qty'] * o['price'], 0)} DAI｜{tag}｜剩 {left_text(o['left'])}")
        if len(lst) > MAX_LINES:
            lines.append(f"… 还有 {len(lst) - MAX_LINES} 张，网站上看全部")
        lines.append("")

    if sells:
        block("卖单", "📤", sells)
    if buys:
        block("买单", "📥", buys)
    lines.append("✅ 智能合约担保，一笔成交，到期没成交自动退回")
    lines.append(random.choice(URGE))
    lines.append(f"👉 立即成交：{SITE}")
    return "\n".join(lines)


def send(text):
    key, secret, gid = os.environ.get("DEBOX_API_KEY"), os.environ.get("DEBOX_API_SECRET", ""), os.environ.get("DEBOX_GROUP_ID")
    if not key or not gid:
        print("还没设置 DEBOX_API_KEY / DEBOX_GROUP_ID，本次跳过")
        return False
    nonce, ts = str(random.randint(0, 2 ** 31 - 1)), str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-API-KEY": key, "nonce": nonce, "timestamp": ts,
               "X-Request-Id": str(uuid.uuid4()), "User-Agent": "ailgns-otc-broadcast"}
    if secret:
        headers["signature"] = hashlib.sha1(f"{secret}{nonce}{ts}".encode()).hexdigest()
    body = {"chat_id": gid, "chat_type": "group", "content": text, "parse_mode": "text",
            "reply_markup": {"inline_keyboard": [[{"text": "立即成交 →", "url": SITE}]]}}
    req = urllib.request.Request(API, json.dumps(body).encode(), headers)
    r = json.load(urllib.request.urlopen(req, timeout=30))
    if not (r.get("ok") or r.get("success")):
        raise RuntimeError(f"DeBox 发送失败：{r}")
    print("已发到 DeBox 群")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    now = int(rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
    count, orders = open_orders(now)
    last_id = state.get("next_id", count)  # 第一次运行：之前的挂单不算新挂单
    new_ids = {o["id"] for o in orders if o["id"] >= last_id}
    hours = float(os.environ.get("DEBOX_SUMMARY_HOURS") or 2)
    due = time.time() - state.get("last_summary", 0) >= hours * 3600
    print(f"共 {count} 张挂单，正在挂 {len(orders)} 张，新挂单 {sorted(new_ids)}")

    if not orders and not args.test:
        reason = None
    elif args.test:
        reason = "test"
    elif new_ids:
        reason = "new"
    elif due:
        reason = "summary"
    else:
        reason = None
    if reason:
        text = message(orders, market_price(), new_ids) if orders else "📢 AiLGNS OTC 目前没有挂单\n" + random.choice(URGE).replace("手慢无", "第一个挂单的就是你") + f"\n👉 {SITE}"
        print(text)
        if not args.dry_run and send(text):
            state["last_summary"] = time.time()
    if not args.dry_run:
        state["next_id"] = count
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as f:
            json.dump(state, f)


if __name__ == "__main__":
    main()
