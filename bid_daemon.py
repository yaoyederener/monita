"""🤖 做市买单 · 常驻版（2026-11-17 站长要「越快越好」）：和 bid_maker.py 的实盘是同一套规矩，只是不等 5 分钟的定时任务，
而是常驻在新加坡的服务器上（离官方节点 25 毫秒），每出一个块（约 1 秒）看一眼：

  · 钱包里一出现 AiLGNS（= 买单被人吃了）→ 马上走官方路由卖掉（最低到手 = 成本，低于成本这笔自己回滚）→ 补挂买单
  · 没成交时每 CHECK_EVERY 秒核一次买单价，DEX 价动了就撤掉重挂

链本身约 1 秒一个块，所以「成交 → 卖出 → 重新挂单」最快 2～3 秒，做不到毫秒。
规矩全在 bid_maker.live()：只当普通用户、卖出不低于成本、回撤超过 BID_MAX_LOSS 停机、出错停机、挂单 30 分钟自动过期。
停机后不会自己恢复：改 /etc/bidbot.env 里的 BID_RESET 成一个新值，再 systemctl restart bidbot。

配置在 /etc/bidbot.env（只有 root 能读）：ARB_KEY、BID_SIZE、BID_MARGIN、BID_MAX_LOSS、BID_RESET。
⚠️ 和 GitHub 定时任务里的实盘（仓库变量 BID_ON）共用一个钱包，两边只能开一个：用常驻版时 BID_ON 必须是 0 / 不设。
"""
import json
import os
import sys
import time

import chains
import bid_maker as b
from ailgns_lp_monitor import rpc

CHECK_EVERY = int(os.environ.get("BID_CHECK_EVERY", "10"))   # 没成交时多久核一次挂单价（秒）
b.STATE = os.environ.get("BID_STATE", "/var/lib/bidbot/state.json")


def fast_send(chain, acct, to, data, label):
    """同 chains.send_tx，但每 0.3 秒查一次回执（原来 2 秒查一次，对 1 秒一个块的链太慢）"""
    from eth_utils import to_checksum_address
    tx = {"from": acct.address, "to": to, "data": data}
    gas = int(int(rpc("eth_estimateGas", [tx]), 16) * 1.3)
    body = {"to": to_checksum_address(to), "data": data, "value": 0, "chainId": chains.CHAIN_IDS[chain], "gas": gas,
            "gasPrice": int(rpc("eth_gasPrice", []), 16),
            "nonce": int(rpc("eth_getTransactionCount", [acct.address, "pending"]), 16)}
    raw = acct.sign_transaction(body).raw_transaction.hex()
    h = rpc("eth_sendRawTransaction", [raw if raw.startswith("0x") else "0x" + raw])
    for _ in range(200):
        r = rpc("eth_getTransactionReceipt", [h])
        if r:
            if r["status"] != "0x1":
                raise RuntimeError(f"{label}失败: {h}")
            return h
        time.sleep(0.3)
    raise RuntimeError(f"{label} 1 分钟未确认: {h}")


chains.send_tx = fast_send


def save(st):
    os.makedirs(os.path.dirname(b.STATE), exist_ok=True)
    tmp = b.STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, b.STATE)


def main():
    if not os.environ.get("ARB_KEY", "").strip():
        raise SystemExit("没有 ARB_KEY（/etc/bidbot.env），不跑")
    print(f"[常驻] 启动：每轮最多 {b.SIZE:g} 枚 · 余量 {b.MARGIN * 100:.3f}% · 回撤 {b.MAX_LOSS:g} DAI 停机 · 每 {CHECK_EVERY}s 核一次挂单价", flush=True)
    st = b.load()
    last_blk, last_full, errs = 0, 0.0, 0
    while True:
        try:
            blk = int(rpc("eth_blockNumber", []), 16)
            if blk != last_blk:
                last_blk = blk
                now = time.time()
                halted = bool(st.get("halted"))
                got = (not halted) and b.bal(b.AIL, b.ME) >= b.MIN_SELL
                if got or now - last_full >= (60 if halted else CHECK_EVERY):
                    if got:
                        print(f"[常驻] 块 {blk}：钱包里有 AiLGNS 了，马上处理", flush=True)
                    b.live(st)
                    st["live_at"] = int(time.time())
                    save(st)
                    last_full = time.time()
                    sys.stdout.flush()
            errs = 0
        except SystemExit:
            raise
        except Exception as e:  # 节点偶尔抖一下：记一笔，稍等再来；连着错很多次就慢下来，别狂刷
            errs += 1
            print(f"[常驻] 出错（第 {errs} 次）：{str(e)[:200]}", flush=True)
            try:
                save(st)
            except Exception:
                pass
            time.sleep(min(30, 2 * errs))
        time.sleep(0.5)


if __name__ == "__main__":
    main()
