import unittest

from ibs_daily_funds_monitor import (
    build_report,
    default_bucket,
    default_state,
    ensure_bucket,
    reconstruct_balances,
    record_trade,
    record_transfer,
)


class Trade:
    def __init__(self, side, ibs_raw, usdt_raw):
        self.side = side
        self.ibs_raw = ibs_raw
        self.usdt_raw = usdt_raw


class DailyFundsMonitorTests(unittest.TestCase):
    def test_trades_are_counted_by_direction(self):
        bucket = default_bucket()
        record_trade(bucket, Trade("BUY", 10, 25))
        record_trade(bucket, Trade("SELL", 7, 20))
        self.assertEqual(bucket["buy_count"], 1)
        self.assertEqual(bucket["sell_count"], 1)
        self.assertEqual(bucket["buy_usdt_raw"], "25")
        self.assertEqual(bucket["sell_usdt_raw"], "20")

    def test_internal_transfer_is_not_external_spending(self):
        bucket = default_bucket()
        from ibs_daily_funds_monitor import TREASURY_ADDRESSES, RBS_ADDRESSES
        record_transfer(bucket, next(iter(TREASURY_ADDRESSES.values())), next(iter(RBS_ADDRESSES.values())), 100, "0x1")
        self.assertEqual(bucket["treasury_internal_out_raw"], "100")
        self.assertEqual(bucket["rbs_internal_in_raw"], "100")
        self.assertEqual(bucket["treasury_external_out_raw"], "0")
        self.assertEqual(bucket["internal_usdt_raw"], "100")

    def test_external_outflow_is_recorded(self):
        bucket = default_bucket()
        from ibs_daily_funds_monitor import TREASURY_ADDRESSES
        record_transfer(bucket, next(iter(TREASURY_ADDRESSES.values())), "0x0000000000000000000000000000000000000001", 55, "0x2")
        self.assertEqual(bucket["treasury_external_out_raw"], "55")
        self.assertEqual(len(bucket["large_external_outflows"]), 1)

    def test_balances_are_reconstructed_backwards_from_transfers(self):
        state = default_state()
        bucket = ensure_bucket(state, "2026-08-17")
        bucket["lp_total_in_raw"] = "250"
        bucket["lp_total_out_raw"] = "100"
        bucket["treasury_total_out_raw"] = "20"
        reconstruct_balances(state, {"lp": 1150, "treasury": 980, "rbs": 500})
        self.assertEqual(bucket["opening_balances_raw"]["lp"], "1000")
        self.assertEqual(bucket["opening_balances_raw"]["treasury"], "1000")
        self.assertEqual(bucket["opening_balances_raw"]["rbs"], "500")

    def test_report_separates_trade_and_actual_lp_change(self):
        bucket = default_bucket()
        bucket.update({
            "buy_count": 2,
            "sell_count": 1,
            "buy_usdt_raw": str(200 * 10**18),
            "sell_usdt_raw": str(250 * 10**18),
            "opening_balances_raw": {"lp": str(1000 * 10**18), "treasury": str(500 * 10**18), "rbs": str(300 * 10**18)},
            "closing_balances_raw": {"lp": str(940 * 10**18), "treasury": str(490 * 10**18), "rbs": str(300 * 10**18)},
        })
        message = build_report("2026-08-17", bucket, 18, 18)
        self.assertIn("交易净流量：<b>-50.00 USDT</b>", message)
        self.assertIn("LP实际变化：<b>-60.00 USDT</b>", message)
        self.assertIn("项目总净消耗：<b>70.00 USDT</b>", message)


if __name__ == "__main__":
    unittest.main()
