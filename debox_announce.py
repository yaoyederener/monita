"""往营销群发一条通告（站长手动触发）：把 announcements/ 里的文字原样发到 DeBox 营销群。
需要的环境变量：DEBOX_API_KEY、DEBOX_API_SECRET（可选）
    python debox_announce.py announcements/ilo-otc.txt
    python debox_announce.py announcements/ilo-otc.txt --dry-run   # 只打印，不发
"""
import argparse
import os

import otc_debox_broadcast as dbx

GROUPS = ["oes8etcm", "fvg4wksw"]  # 营销群（和网站 functions/api/otc-watch.js 的 GROUPS 一样）


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    text = open(args.file, encoding="utf-8").read().strip()
    print(text)
    if args.dry_run:
        return
    dbx.SITE = text.rsplit("🧬", 1)[-1].strip() if "🧬" in text else dbx.SITE  # 「立即成交」按钮跳到通告里的链接
    failed = 0
    for g in GROUPS:
        os.environ["DEBOX_GROUP_ID"] = g
        try:
            dbx.send(text)
            print(f"已发到群 {g}")
        except Exception as e:
            failed += 1
            print(f"群 {g} 发送失败：{e}")
    raise SystemExit(1 if failed == len(GROUPS) else 0)


if __name__ == "__main__":
    main()
