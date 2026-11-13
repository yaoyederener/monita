"""🔥 AiLGNS 代烧机器人（站长 2026-11-14 定「做机器人代烧」）
神算子大奖要烧上千个 .ailgns 名字，普通钱包只能一个一个确认。客户在销毁页：
① 在域名合约上把名字的操作权授给本钱包（setApprovalForAll，随时能取消）② 钱包签名委托 → 网站存成待办（清单在下单时定死）。
本脚本每 5 分钟：取待办 → 自己再核一遍签名 → 只把「清单里、现在还在客户钱包里、不是主名字」的名字转进黑洞 → 烧够就停 → 回报网站。
安全边界（每一条都在本脚本里自己核，不只信网站）：
  - 委托必须有客户钱包的签名（文字里写明数量和不动的名字）；清单不超过签名的数量
  - 主名字从链上读，永远不烧；只转进黑洞地址，不会转去别处
  - 烧了几个按链上黑洞记录数（从接单那个区块起），够数就停，不会多烧
  - 客户取消授权后一个都动不了；本钱包只放 Gas，不碰任何合约管理权限
需要：REG_KEY（本钱包私钥，和代注册同一个）、BACKUP_KEY（读写网站待办）。
    python burn_bot.py --dry-run     只看会烧哪些，不发交易
    python burn_bot.py --selftest    不联网，测挑名字的规则
"""
import argparse
import json
import os
import time
import urllib.request

NAMES = "0xbc408e9d7350967e7247c917a086cb4ece0ed988"
DEAD = "0x000000000000000000000000000000000000dead"
T_XFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SITE = "https://ailgns.dev/api/burn-order"
OWNED = "https://ailgns.dev/api/name-owned?a="
CHAIN_ID = 6714
BATCH = 50          # 一批发 50 笔
RUN_SECONDS = 150   # 一轮最多干 150 秒（整个定时任务 10 分钟上限，后面还有别的步骤）
RUN_MAX = 600       # 一轮最多烧 600 个
GAS_KEEP = 2.0      # 本钱包至少留 2 DAI 给代注册用


def pad(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def burn_text(n, keep, a, day):  # 和网站 functions/api/burn-order.js、genesis.js 一字不差
    return ("AiLGNS 代销毁委托\n请机器人把我钱包里的 " + str(n) + " 个 .ailgns 名字转进黑洞地址（永久销毁，拿不回来），用来领取预言家大奖。\n不动的名字："
            + ((keep + ".ailgns") if keep else "无") + "\n我的地址：" + a + "\n日期：" + day)


def pick(order_ids, n, owned_now, labels, keep, burned):
    """挑这一轮能烧的名字（纯函数，--selftest 测它）。
    order_ids 委托清单；n 签名的数量；owned_now 现在还在客户钱包里的 id 集合；labels id→名字；keep 主名字；burned 接单后链上已进黑洞的个数。"""
    left = max(0, n - burned)
    out = []
    for i in order_ids[:n]:  # 清单超过签名数量的部分一律不碰
        if len(out) >= left:
            break
        lb = labels.get(i)
        if i not in owned_now or lb is None or (keep and lb == keep):
            continue  # 不在客户钱包里 / 网站清单里查不到名字 / 是主名字：不烧
        out.append(i)
    return out


def selftest():
    ids = ["%064x" % i for i in range(1, 11)]
    labels = {i: "n%d" % k for k, i in enumerate(ids)}
    own = set(ids)
    assert pick(ids, 10, own, labels, "", 0) == ids
    assert pick(ids, 4, own, labels, "", 0) == ids[:4], "不超过签名的数量"
    assert pick(ids, 10, own, labels, "", 7) == ids[:3], "链上已经烧了 7 个，只再烧 3 个"
    assert pick(ids, 10, own, labels, "", 10) == [] and pick(ids, 10, own, labels, "", 99) == [], "烧够就停"
    assert pick(ids, 10, own, labels, "n2", 0) == [i for i in ids if labels[i] != "n2"], "主名字不烧"
    assert pick(ids, 10, own - {ids[0], ids[5]}, labels, "", 0) == [i for i in ids if i not in (ids[0], ids[5])], "不在钱包里的跳过"
    lab2 = dict(labels); del lab2[ids[3]]
    assert ids[3] not in pick(ids, 10, own, lab2, "", 0), "查不到名字的不烧"
    assert pick(ids + ["%064x" % 99], 10, own | {"%064x" % 99}, dict(labels, **{"%064x" % 99: "x"}), "", 0) == ids, "清单多出来的不碰"
    t = burn_text(1004, "baggy", "0xabc", "2026-11-14")
    assert "1004 个" in t and "不动的名字：baggy.ailgns" in t and burn_text(3, "", "0xabc", "2026-11-14").count("不动的名字：无") == 1
    print("selftest 全过")


def http(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, headers={"content-type": "application/json", "user-agent": "ailgns-keeper"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def rpc_batch(calls):
    """一次请求发一串 [(method, params)]，按顺序返回结果；单条出错的位置是 None。"""
    from ailgns_lp_monitor import RPCS
    body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(RPCS[attempt % len(RPCS)], json.dumps(body).encode(), {"Content-Type": "application/json", "User-Agent": "ailgns-monitor"})
            r = json.load(urllib.request.urlopen(req, timeout=60))
            if not isinstance(r, list):
                raise RuntimeError(str(r)[:200])
            by = {x.get("id"): x for x in r}
            return [(by.get(i) or {}).get("result") for i in range(len(calls))]
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"批量 RPC 失败: {last}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    dry = args.dry_run
    from eth_account import Account
    from eth_account.messages import encode_defunct
    from chains import rpc
    key = os.environ.get("BACKUP_KEY")
    acct = Account.from_key(os.environ["REG_KEY"]) if os.environ.get("REG_KEY") else None
    if not key or not acct:
        print("缺 REG_KEY / BACKUP_KEY，跳过")
        return
    me = acct.address.lower()
    orders = http(f"{SITE}?key={key}").get("list") or []
    if not orders:
        print("没有代烧待办")
        return
    t0 = time.time()
    for o in orders[:3]:
        a, n, keep, ids = o["a"].lower(), int(o["n"]), str(o.get("keep") or ""), [str(x).lower() for x in (o.get("ids") or [])]
        say = lambda **kw: None if dry else http(SITE, dict({"action": "progress", "key": key, "id": o["id"]}, **kw))
        print(f"代烧委托 {o['id']} {a} 要烧 {n} 个，清单 {len(ids)} 个")
        try:
            # ① 自己核签名：必须是这个钱包签的、文字里的数量和主名字就是委托里的
            who = Account.recover_message(encode_defunct(text=burn_text(n, keep, a, o["day"])), signature=o["sig"]).lower()
            if who != a or not (1 <= n <= 3000) or len(ids) > n or any(len(i) != 64 for i in ids):
                print("  ❌ 签名或清单对不上，作废"); say(state="done", note="委托核对不上，已作废，一个都没烧"); continue
            # ② 主名字从链上读（不信委托里写的）：链上有主名字而委托里写的不一样 → 不干
            r = rpc("anubis", "eth_call", [{"to": NAMES, "data": "0xf5c57382" + pad(a)}, "latest"])[2:]
            off = int(r[:64], 16) * 2; ln = int(r[off:off + 64], 16)
            prim = bytes.fromhex(r[off + 64:off + 64 + ln * 2]).decode("utf-8") if ln else ""
            if prim and prim != keep:
                print(f"  ❌ 链上主名字 {prim} 和委托里不动的名字 {keep} 不一样，不烧"); say(note="主名字变了，没有烧。请取消授权后重新委托"); continue
            # ③ 客户还授权着吗
            if int(rpc("anubis", "eth_call", [{"to": NAMES, "data": "0xe985e9c5" + pad(a) + pad(me)}, "latest"]), 16) != 1:
                print("  ⏳ 客户没授权（或已取消），这轮不动"); say(note="还没收到授权（或已取消授权），机器人没有动"); continue
            # ④ 接单区块：之后「烧了几个」只数链上记录（客户自己手点的也算），够数就停
            head = int(rpc("anubis", "eth_blockNumber", []), 16)
            blk0 = int(o.get("blk0") or 0)
            if not blk0:
                blk0 = head; say(blk0=blk0)
            burned = 0
            for s in range(blk0, head + 1, 5000):
                burned += len(rpc("anubis", "eth_getLogs", [{"address": NAMES, "fromBlock": hex(s), "toBlock": hex(min(head, s + 4999)), "topics": [T_XFER, "0x" + pad(a), "0x" + pad(DEAD)]}]))
            if burned >= n:
                print(f"  ✅ 链上已烧 {burned} 个，够数"); say(state="done", done=n); continue
            # ⑤ 现在还在客户钱包里的（链上逐个问 ownerOf）+ 名字表（认主名字用）
            own_list = http(OWNED + a).get("list") or []
            labels = {str(x["id"]).lower(): x.get("label") for x in own_list if x.get("id")}
            owned_now = set()
            for i in range(0, len(ids), 100):
                grp = ids[i:i + 100]
                for tid, res in zip(grp, rpc_batch([("eth_call", [{"to": NAMES, "data": "0x6352211e" + tid}, "latest"]) for tid in grp])):
                    if res and res[-40:].lower() == a[2:]:
                        owned_now.add(tid)
            todo = pick(ids, n, owned_now, labels, prim or keep, burned)
            if not todo:
                print(f"  清单里没有能烧的了（已烧 {burned}）"); say(state="done", done=burned, note="清单里的名字已经不在你钱包里了" if burned < n else ""); continue
            todo = todo[:RUN_MAX]
            print(f"  已烧 {burned}，这轮计划烧 {len(todo)} 个")
            if dry:
                print("  dry：", "、".join(labels.get(t, "?") for t in todo[:10]), "…"); continue
            # ⑥ 发交易：一批 50 笔，连号 nonce，等这一批都上链再发下一批
            data = lambda tid: "0x23b872dd" + pad(a) + pad(DEAD) + tid
            gas = int(int(rpc("anubis", "eth_estimateGas", [{"from": me, "to": NAMES, "data": data(todo[0])}]), 16) * 1.5)
            price = int(rpc("anubis", "eth_gasPrice", []), 16)
            bal = int(rpc("anubis", "eth_getBalance", [acct.address, "latest"]), 16)
            can = int((bal - GAS_KEEP * 1e18) // (gas * price)) if bal > GAS_KEEP * 1e18 else 0
            if can < 1:
                print("  ⚠️ 机器人钱包 Gas 不够，这轮不烧"); say(note="机器人钱包 Gas 不够，团队补上后自动继续"); continue
            todo = todo[:can]
            ok = fail = 0
            from eth_utils import to_checksum_address
            for i in range(0, len(todo), BATCH):
                if time.time() - t0 > RUN_SECONDS:
                    print("  ⏱ 这轮时间到，下轮接着烧"); break
                grp = todo[i:i + BATCH]
                nonce = int(rpc("anubis", "eth_getTransactionCount", [acct.address, "pending"]), 16)
                raws = []
                for k, tid in enumerate(grp):
                    raw = acct.sign_transaction({"to": to_checksum_address(NAMES), "data": data(tid), "value": 0, "chainId": CHAIN_ID, "gas": gas, "gasPrice": price, "nonce": nonce + k}).raw_transaction.hex()
                    raws.append(raw if raw.startswith("0x") else "0x" + raw)
                hs = rpc_batch([("eth_sendRawTransaction", [x]) for x in raws])
                sent = [h for h in hs if h]
                if len(sent) < len(grp):
                    print(f"  ⚠️ 这批 {len(grp)} 笔只发出去 {len(sent)} 笔")
                end = nonce + len(sent)
                for _ in range(45):  # 等这一批全部上链（最多约 90 秒）
                    if int(rpc("anubis", "eth_getTransactionCount", [acct.address, "latest"]), 16) >= end:
                        break
                    time.sleep(2)
                rc = rpc_batch([("eth_getTransactionReceipt", [h]) for h in sent]) if sent else []
                good = sum(1 for x in rc if x and x.get("status") == "0x1")
                ok += good; fail += len(grp) - good
                print(f"  批 {i // BATCH + 1}：成功 {good} / {len(grp)}")
                if good < len(grp):
                    break  # 有没成功的：停下，下轮重新核对再烧（不会重复烧，ownerOf 会挡住）
            total = burned + ok
            print(f"  这轮烧了 {ok} 个，累计 {total} / {n}")
            say(done=min(n, total), fail=fail, **({"state": "done"} if total >= n else {"note": "机器人正在烧，每 5 分钟一轮"}))
        except Exception as e:
            print(f"  ❌ 出错，这轮跳过：{e}")
        if time.time() - t0 > RUN_SECONDS:
            break


if __name__ == "__main__":
    main()
