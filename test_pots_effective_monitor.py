import unittest
from datetime import datetime, timedelta, timezone

import pots_effective_monitor as monitor
from test_usdt_balance_monitor import make_snapshot


USDT_SCALE = 10**18
IBS_SCALE = 10**18


class EffectiveMonitorTests(unittest.TestCase):
    def test_interval_uses_immediately_previous_observation(self):
        now = datetime(2026, 8, 13, 12, 5, tzinfo=timezone.utc)
        previous_snapshot = make_snapshot(now - timedelta(minutes=5), 100)
        previous = monitor.snapshot_record(previous_snapshot)
        current = make_snapshot(now, 200, lp=9_990_000, rbs=1_490_000, safety=205_000, supply=1_001_000)
        change = monitor.interval_change(previous, current)
        self.assertEqual(change.elapsed_seconds, 300)
        self.assertEqual(change.lp_delta_raw, -10_000 * USDT_SCALE)
        self.assertEqual(change.rbs_delta_raw, -10_000 * USDT_SCALE)
        self.assertEqual(change.safety_delta_raw, 5_000 * USDT_SCALE)
        self.assertEqual(change.treasury_delta_raw, -15_000 * USDT_SCALE)
        self.assertEqual(change.supply_delta_raw, 1_000 * IBS_SCALE)

    def test_runway_annualizes_this_interval_only(self):
        days = monitor.runway_days(1_000_000 * USDT_SCALE, -10_000 * USDT_SCALE, 6 * 3600)
        self.assertEqual(days, monitor.Decimal("25"))
        self.assertIsNone(monitor.runway_days(1_000_000, 1, 300))

    def test_solvency_separates_spot_from_full_exit_price(self):
        current = make_snapshot(
            datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
            200,
            lp=10_000_000,
            rbs=1_500_000,
            safety=500_000,
            supply=900_000,
            price=25,
        )
        metrics = monitor.calculate_solvency(current)
        self.assertEqual(metrics.external_circulating_ibs, monitor.Decimal("400000"))
        self.assertEqual(metrics.spot_liability_usdt, monitor.Decimal("10000000"))
        self.assertLess(metrics.lp_full_exit_price, current.ibs_price_usdt)
        self.assertEqual(metrics.visible_backing_price, monitor.Decimal("13.333333333333333333333333333333333333333333333333333333333333333333333333333333"))
        self.assertAlmostEqual(float(metrics.spot_coverage_ratio), 12_000_000 / 10_000_000)

    def test_report_is_immediate_for_rbs_or_supply_but_not_lp_noise(self):
        now = datetime(2026, 8, 13, 12, 5, tzinfo=timezone.utc)
        previous = monitor.snapshot_record(make_snapshot(now - timedelta(minutes=5), 100))
        lp_only = make_snapshot(now, 200, lp=9_999_999)
        due, _ = monitor.report_due(previous, lp_only, monitor.interval_change(previous, lp_only))
        self.assertFalse(due)
        rbs_change = make_snapshot(now, 200, rbs=1_499_999)
        due, reason = monitor.report_due(previous, rbs_change, monitor.interval_change(previous, rbs_change))
        self.assertTrue(due)
        self.assertIn("RBS", reason)

    def test_risk_is_high_when_treasury_falls_while_supply_grows(self):
        now = datetime(2026, 8, 13, 12, 5, tzinfo=timezone.utc)
        previous = monitor.snapshot_record(make_snapshot(now - timedelta(minutes=5), 100))
        current = make_snapshot(now, 200, lp=9_900_000, supply=1_001_000)
        change = monitor.interval_change(previous, current)
        solvency = monitor.calculate_solvency(current)
        label, reasons = monitor.classify_risk(current, change, solvency)
        self.assertEqual(label, "🔴 高风险")
        self.assertIn("资金减少与增发同时发生", reasons)

    def test_message_has_no_24h_or_ordinary_trade_statistics(self):
        now = datetime(2026, 8, 13, 12, 5, tzinfo=timezone.utc)
        previous = monitor.snapshot_record(make_snapshot(now - timedelta(minutes=5), 100))
        current = make_snapshot(now, 200, lp=9_990_000, supply=1_001_000)
        message = monitor.build_message(current, monitor.interval_change(previous, current))
        self.assertIn("与上次报告间隔", message)
        self.assertIn("全部池外IBS一次卖入现有LP", message)
        self.assertIn("按现价可完全覆盖", message)
        self.assertNotIn("24h", message)
        self.assertNotIn("大额买卖", message)
        self.assertNotIn("毛卖压", message)


if __name__ == "__main__":
    unittest.main()
