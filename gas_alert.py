"""触发钱包 Gas 余额提醒：Anubis 链 DAI 或 Polygon 链 POL 低于下限，发邮件提醒站长补充。
低的时候每天最多提醒一次；补够以后自动停止提醒。
（回购分账在触发钱包 DAI 低于 2 时会从收入里自动补 0.5，所以 Anubis 一般不会见底，但收入少的时候也会不够）
需要的环境变量：SMTP_USER / SMTP_PASS / NOTIFY_EMAIL
"""
import json
import os
import time

from chains import rpc
from notify_mail import send as send_mail

KEEPER = "0x89Dd06754bb239b6C4C92Bc47C8Fa72889084E27"
DAI = "0x83fd06f0846d9d90b3016bf670efe2e0b11cde14"  # Anubis 上付 Gas 的 DAI
LOW = {"anubis": 5.0, "polygon": 10.0}  # 低于这么多就提醒
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "gas_alert.json")


def main():
    anubis = int(rpc("anubis", "eth_call", [{"to": DAI, "data": "0x70a08231" + KEEPER[2:].lower().rjust(64, "0")}, "latest"]), 16) / 1e18
    polygon = int(rpc("polygon", "eth_getBalance", [KEEPER, "latest"]), 16) / 1e18
    low = []
    if anubis < LOW["anubis"]:
        low.append(f"Anubis 链：剩 {anubis:.2f} DAI（低于 {LOW['anubis']:g}），请转 DAI 代币（0x83fd…De14）过去")
    if polygon < LOW["polygon"]:
        low.append(f"Polygon 链：剩 {polygon:.2f} POL（低于 {LOW['polygon']:g}），请转 POL 过去")
    print(f"触发钱包 Gas：Anubis {anubis:.2f} DAI，Polygon {polygon:.2f} POL" + ("　← 不够了" if low else ""))
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    day = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))
    if not low:
        if state:
            _save({})
        return
    if state.get("day") == day:
        return
    send_mail("⛽ AiLGNS 触发钱包 Gas 不够了", f"触发钱包 {KEEPER} 的 Gas 快用完了：\n\n" + "\n".join(low)
              + "\n\nGas 用完后，自动退还过期挂单、收入回购分账都会停下来。补够后这个提醒会自动停止。\n\n—— AiLGNS 服务站")
    _save({"day": day})


def _save(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(state, f)


if __name__ == "__main__":
    main()
