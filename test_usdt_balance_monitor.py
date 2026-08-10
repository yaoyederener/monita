import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import usdt_balance_monitor as monitor


USDT_SCALE = 10**18
IBS_SCALE = 10**18


def swap_data(amount0_in, amount1_in, amount0_out, amount1_out):
    return b"".join(
        int(value).to_bytes(32, byteorder="big")
        for value in (amount0_in, amount1_in, amount0_out, amount1_out)
    )


def swap_log(
    amount0_in,
    amount1_in,
    amount0_out,
    amount1_out,
    *,
    block=200,
    transaction_index=0,
    log_index=0,
    tx_byte="11",
):
    return {
        "data": swap_data(amount0_in, amount1_in, amount0_out, amount1_out),
        "transactionHash": "0x" + tx_byte * 32,
        "blockNumber": block,
        "transactionIndex": transaction_index,
        "logIndex": log_index,
    }


def make_snapshot(
    when,
    block,
    lp=10_000_000,
    lp_ibs=500_000,
    lp_balance=None,
    rbs=1_500_000,
    safety=200_000,
    protocol=None,
    supply=1_000_000,
    backing=monitor.Decimal("11.7"),
    price=monitor.Decimal("20"),
    config_hash="config-a",
):
    lp_balance = lp if lp_balance is None else lp_balance
    protocol = lp_balance + rbs + safety if protocol is None else protocol
    return monitor.CurrentSnapshot(
        observed_at=when,
        block_time_utc=when.isoformat(),
        block_number=block,
        usdt_decimals=18,
        ibs_decimals=18,
        lp_usdt_raw=lp * USDT_SCALE,
        lp_ibs_raw=lp_ibs * IBS_SCALE,
        lp_balance_usdt_raw=lp_balance * USDT_SCALE,
        rbs_usdt_raw=rbs * USDT_SCALE,
        safety_usdt_raw=safety * USDT_SCALE,
        treasury_usdt_raw=(lp + rbs + safety) * USDT_SCALE,
        total_usdt_raw=(lp + rbs) * USDT_SCALE,
        protocol_usdt_raw=protocol * USDT_SCALE,
        ibs_total_supply_raw=supply * IBS_SCALE,
        ibs_dead_raw=0,
        ibs_circulating_raw=supply * IBS_SCALE,
        ibs_is_token0=True,
        ibs_price_usdt=price,
        market_cap_usdt=price * monitor.Decimal(supply),
        backing_per_ibs=backing,
        treasury_address="0xac739056e611d639aBEb0B9a87Da38Bd297Ba00E",
        tax_treasury_address="0xD9F49Fa9d8376041093DDA76Baaf02c3221f8702",
        protocol_addresses=("0x01", "0x02"),
        protocol_config_hash=config_hash,
    )


def make_metrics(
    rbs_delta,
    elapsed_days,
    total_change=0,
    backing_change=0,
    external=0,
    treasury_delta=None,
    supply_delta=0,
    start_block=100,
    end_block=200,
):
    treasury_delta_raw = (
        int(total_change * 1_000_000)
        if treasury_delta is None
        else treasury_delta * USDT_SCALE
    )
    treasury_change = (
        monitor.Decimal(str(total_change))
        if treasury_delta is None
        else monitor.Decimal(str(treasury_delta / 12_000_000))
    )
    return monitor.WindowMetrics(
        label="test",
        elapsed_days=monitor.Decimal(str(elapsed_days)),
        total_delta_raw=int(total_change * 1_000_000),
        total_change=monitor.Decimal(str(total_change)),
        treasury_delta_raw=treasury_delta_raw,
        treasury_change=treasury_change,
        rbs_delta_raw=rbs_delta * USDT_SCALE,
        rbs_change=monitor.Decimal(str(rbs_delta / 1_500_000)),
        supply_delta_raw=supply_delta * IBS_SCALE,
        supply_change=monitor.Decimal("0"),
        backing_change=monitor.Decimal(str(backing_change)),
        external_net_raw=external,
        start_block=start_block,
        end_block=end_block,
    )


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        temp_root = Path(self._temp_dir.name)
        self._state_file_patch = patch.object(
            monitor,
            "STATE_FILE",
            temp_root / "data" / "usdt_balance.json",
        )
        self._history_file_patch = patch.object(
            monitor,
            "HISTORY_FILE",
            temp_root / "data" / "usdt_flow_history.csv",
        )
        self._state_file_patch.start()
        self._history_file_patch.start()

    def tearDown(self):
        self._history_file_patch.stop()
        self._state_file_patch.stop()
        self._temp_dir.cleanup()

    def test_legacy_rbs_state_is_preserved_for_migration(self):
        legacy = {
            "wallet": monitor.RBS_ADDRESS,
            "raw_balance": "1558901876095307582181485",
            "block_number": 114301586,
            "updated_at_utc": "2026-08-06T06:00:39+00:00",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(legacy), encoding="utf-8")
            with patch.object(monitor, "STATE_FILE", state_path):
                loaded = monitor.load_state()
        self.assertIsNone(loaded["latest"])
        self.assertEqual(loaded["legacy_rbs_raw_balance"], legacy["raw_balance"])

    def test_protocol_balance_delta_excludes_internal_transfer(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        previous = make_snapshot(now - timedelta(hours=1), 100)
        current = make_snapshot(
            now,
            200,
            lp=10_000_000,
            lp_balance=10_000_500,
            rbs=1_499_700,
            safety=200_300,
        )
        previous_record = monitor.record_from_snapshot(
            previous, monitor.FlowSummary(100, 100, False)
        )
        flow = monitor.summarize_interval(previous_record, current)
        self.assertTrue(flow.complete)
        self.assertEqual(flow.external_net_raw, 500 * USDT_SCALE)

    def test_lp_core_uses_reserve_but_perimeter_uses_token_balance(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        snapshot = make_snapshot(now, 100, lp=10_000_000, lp_balance=10_000_123)
        self.assertEqual(snapshot.total_usdt_raw, 11_500_000 * USDT_SCALE)
        self.assertEqual(snapshot.treasury_usdt_raw, 11_700_000 * USDT_SCALE)
        self.assertEqual(snapshot.protocol_usdt_raw, 11_700_123 * USDT_SCALE)

    def test_market_data_uses_reserves_decimals_and_circulating_supply(self):
        price, market_cap = monitor.calculate_market_data(
            2_000_000 * 10**6,
            100_000 * IBS_SCALE,
            6,
            18,
            900_000 * IBS_SCALE,
        )
        self.assertEqual(price, monitor.Decimal("20"))
        self.assertEqual(market_cap, monitor.Decimal("18000000"))

    def test_market_data_rejects_empty_ibs_reserve(self):
        with self.assertRaisesRegex(RuntimeError, "IBS储备为0"):
            monitor.calculate_market_data(
                2_000_000 * USDT_SCALE, 0, 18, 18, 900_000 * IBS_SCALE
            )

    def test_protocol_config_change_rebuilds_flow_baseline(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        previous = make_snapshot(now - timedelta(hours=1), 100, config_hash="old")
        current = make_snapshot(
            now, 200, protocol=12_700_000, config_hash="new"
        )
        previous_record = monitor.record_from_snapshot(
            previous, monitor.FlowSummary(100, 100, False)
        )
        flow = monitor.summarize_interval(previous_record, current)
        self.assertTrue(flow.config_changed)
        self.assertFalse(flow.complete)
        self.assertEqual(flow.external_net_raw, 0)
        self.assertEqual(monitor.report_due({"latest": previous_record}, current, flow)[1], "PROTOCOL_CONFIG_CHANGED")

    def test_large_rbs_balance_drop_is_critical(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        previous = make_snapshot(now - timedelta(minutes=5), 100)
        current = make_snapshot(
            now,
            200,
            rbs=1_350_000,
            protocol=11_550_000,
        )
        previous_record = monitor.record_from_snapshot(
            previous, monitor.FlowSummary(100, 100, False)
        )
        with patch.object(monitor, "CRITICAL_OUTFLOW_USDT", monitor.Decimal("100000")):
            flow = monitor.summarize_interval(previous_record, current)
        self.assertEqual(len(flow.critical_events), 1)
        self.assertEqual(flow.critical_events[0]["name"], "RBS")
        self.assertEqual(flow.external_net_raw, -150_000 * USDT_SCALE)

    def test_24h_window_uses_combined_and_protocol_balances(self):
        end = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        start_snapshot = make_snapshot(
            end - timedelta(hours=24), 100, lp=10_000_000, rbs=1_500_000
        )
        end_snapshot = make_snapshot(
            end, 200, lp=10_100_000, rbs=1_450_000
        )
        start_record = monitor.record_from_snapshot(
            start_snapshot, monitor.FlowSummary(100, 100, False)
        )
        end_record = monitor.record_from_snapshot(
            end_snapshot, monitor.FlowSummary(101, 200, True)
        )
        metrics = monitor.calculate_window(
            [start_record, end_record], end_record, "24h", timedelta(hours=24)
        )
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics.total_delta_raw, 50_000 * USDT_SCALE)
        self.assertEqual(metrics.external_net_raw, 50_000 * USDT_SCALE)
        self.assertEqual(metrics.rbs_delta_raw, -50_000 * USDT_SCALE)

    def test_window_adds_safety_to_treasury_without_changing_legacy_total(self):
        end = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        start = make_snapshot(end - timedelta(hours=24), 100, safety=200_000)
        current = make_snapshot(end, 200, safety=250_000)
        start_record = monitor.record_from_snapshot(
            start, monitor.FlowSummary(100, 100, False)
        )
        # Simulate a state record written before treasury_usdt_raw was introduced.
        start_record.pop("treasury_usdt_raw")
        current_record = monitor.record_from_snapshot(
            current, monitor.FlowSummary(101, 200, True)
        )
        metrics = monitor.calculate_window(
            [start_record, current_record], current_record, "24h", timedelta(hours=24)
        )
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics.total_delta_raw, 0)
        self.assertEqual(metrics.treasury_delta_raw, 50_000 * USDT_SCALE)

    def test_window_rejects_stale_snapshot(self):
        end = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        stale = make_snapshot(end - timedelta(days=5), 100)
        current = make_snapshot(end, 200)
        stale_record = monitor.record_from_snapshot(
            stale, monitor.FlowSummary(100, 100, False)
        )
        current_record = monitor.record_from_snapshot(
            current, monitor.FlowSummary(101, 200, True)
        )
        metrics = monitor.calculate_window(
            [stale_record, current_record], current_record, "24h", timedelta(hours=24)
        )
        self.assertIsNone(metrics)

    def test_window_rejects_a_to_b_to_a_protocol_config_change(self):
        end = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        start = make_snapshot(end - timedelta(hours=24), 100, config_hash="config-a")
        middle = make_snapshot(
            end - timedelta(hours=12), 150, protocol=12_000_000, config_hash="config-b"
        )
        current = make_snapshot(end, 200, protocol=11_900_000, config_hash="config-a")
        records = [
            monitor.record_from_snapshot(start, monitor.FlowSummary(100, 100, False)),
            monitor.record_from_snapshot(
                middle, monitor.FlowSummary(101, 150, False, config_changed=True)
            ),
            monitor.record_from_snapshot(
                current, monitor.FlowSummary(151, 200, False, config_changed=True)
            ),
        ]
        metrics = monitor.calculate_window(
            records, records[-1], "24h", timedelta(hours=24)
        )
        self.assertIsNotNone(metrics)
        self.assertIsNone(metrics.external_net_raw)

    def test_zero_critical_threshold_disables_balance_drop_alert(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        previous = make_snapshot(now - timedelta(minutes=5), 100)
        current = make_snapshot(now, 200, rbs=1_499_999, protocol=11_699_999)
        previous_record = monitor.record_from_snapshot(
            previous, monitor.FlowSummary(100, 100, False)
        )
        with patch.object(monitor, "CRITICAL_OUTFLOW_USDT", monitor.Decimal("0")):
            flow = monitor.summarize_interval(previous_record, current)
        self.assertEqual(flow.critical_events, [])

    def test_runway_uses_faster_24h_drawdown_even_if_7d_grew(self):
        metrics_24h = make_metrics(-100_000, 1)
        metrics_7d = make_metrics(70_000, 7)
        runway = monitor.estimate_rbs_runway(
            1_500_000 * USDT_SCALE, metrics_24h, metrics_7d
        )
        self.assertEqual(runway, monitor.Decimal("15"))

    def test_runway_uses_more_conservative_drawdown_rate(self):
        metrics_24h = make_metrics(-100_000, 1)
        metrics_7d = make_metrics(-350_000, 7)
        runway = monitor.estimate_rbs_runway(
            1_500_000 * USDT_SCALE, metrics_24h, metrics_7d
        )
        self.assertEqual(runway, monitor.Decimal("15"))

    def test_treasury_runway_uses_faster_recent_decline(self):
        metrics_24h = make_metrics(0, 1, treasury_delta=-200_000)
        metrics_7d = make_metrics(0, 7, treasury_delta=-700_000)
        runway = monitor.estimate_treasury_runway(
            12_000_000 * USDT_SCALE, metrics_24h, metrics_7d
        )
        self.assertEqual(runway, monitor.Decimal("60"))

    def test_sell_pressure_counts_only_net_ibs_sells_for_either_pair_order(self):
        token0_logs = [
            {"data": swap_data(100 * IBS_SCALE, 0, 0, 2_000 * USDT_SCALE)},
            {"data": swap_data(0, 1_000 * USDT_SCALE, 50 * IBS_SCALE, 0)},
            {
                "data": swap_data(
                    30 * IBS_SCALE,
                    20 * USDT_SCALE,
                    5 * IBS_SCALE,
                    620 * USDT_SCALE,
                )
            },
        ]
        sell_ibs, sell_usdt, count = monitor.summarize_sell_pressure_logs(
            token0_logs, True
        )
        self.assertEqual(sell_ibs, 125 * IBS_SCALE)
        self.assertEqual(sell_usdt, 2_600 * USDT_SCALE)
        self.assertEqual(count, 2)

        token1_logs = [
            {"data": swap_data(0, 40 * IBS_SCALE, 800 * USDT_SCALE, 0)}
        ]
        self.assertEqual(
            monitor.summarize_sell_pressure_logs(token1_logs, False),
            (40 * IBS_SCALE, 800 * USDT_SCALE, 1),
        )

    def test_large_trade_direction_uses_pair_net_flow_for_both_token_orders(self):
        token0_buy = monitor.decode_large_trade_log(
            swap_log(0, 5_000 * USDT_SCALE, 250 * IBS_SCALE, 0), True
        )
        token0_sell = monitor.decode_large_trade_log(
            swap_log(
                250 * IBS_SCALE,
                0,
                0,
                5_000 * USDT_SCALE,
                log_index=1,
            ),
            True,
        )
        token1_buy = monitor.decode_large_trade_log(
            swap_log(
                5_000 * USDT_SCALE,
                0,
                0,
                250 * IBS_SCALE,
                log_index=2,
            ),
            False,
        )
        token1_sell = monitor.decode_large_trade_log(
            swap_log(
                0,
                250 * IBS_SCALE,
                5_000 * USDT_SCALE,
                0,
                log_index=3,
            ),
            False,
        )

        self.assertEqual(
            (token0_buy.side, token0_buy.ibs_raw, token0_buy.usdt_raw),
            ("BUY", 250 * IBS_SCALE, 5_000 * USDT_SCALE),
        )
        self.assertEqual(
            (token0_sell.side, token0_sell.ibs_raw, token0_sell.usdt_raw),
            ("SELL", 250 * IBS_SCALE, 5_000 * USDT_SCALE),
        )
        self.assertEqual(token1_buy.side, "BUY")
        self.assertEqual(token1_sell.side, "SELL")
        self.assertIsNone(
            monitor.decode_large_trade_log(
                swap_log(250 * IBS_SCALE, 5_000 * USDT_SCALE, 0, 0), True
            )
        )

    def test_large_trade_threshold_is_strictly_over_200_and_dedupes_by_log(self):
        same_tx = "22"
        logs = [
            swap_log(
                0,
                4_000 * USDT_SCALE,
                200 * IBS_SCALE,
                0,
                block=202,
                log_index=0,
                tx_byte=same_tx,
            ),
            swap_log(
                0,
                4_001 * USDT_SCALE,
                200 * IBS_SCALE + 1,
                0,
                block=201,
                transaction_index=2,
                log_index=4,
                tx_byte=same_tx,
            ),
            swap_log(
                201 * IBS_SCALE,
                0,
                0,
                4_020 * USDT_SCALE,
                block=201,
                transaction_index=1,
                log_index=3,
                tx_byte=same_tx,
            ),
            # Exact duplicate returned by an overlapping RPC range.
            swap_log(
                201 * IBS_SCALE,
                0,
                0,
                4_020 * USDT_SCALE,
                block=201,
                transaction_index=1,
                log_index=3,
                tx_byte=same_tx,
            ),
        ]
        trades = monitor.find_large_trades(
            logs,
            True,
            200 * IBS_SCALE,
        )
        self.assertEqual(len(trades), 2)
        self.assertEqual([trade.log_index for trade in trades], [3, 4])
        self.assertEqual([trade.side for trade in trades], ["SELL", "BUY"])
        self.assertNotEqual(trades[0].event_id, trades[1].event_id)

        ignored = monitor.find_large_trades(
            logs,
            True,
            200 * IBS_SCALE,
            [trade.event_id for trade in trades],
        )
        self.assertEqual(ignored, [])

    def test_large_trade_message_contains_amount_price_direction_and_tx_link(self):
        now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        current = make_snapshot(now, 250)
        trade = monitor.decode_large_trade_log(
            swap_log(
                0,
                5_000 * USDT_SCALE,
                250 * IBS_SCALE,
                0,
                block=249,
                log_index=7,
                tx_byte="ab",
            ),
            True,
        )
        message = monitor.build_large_trade_message([trade], current)
        self.assertIn("大额买入", message)
        self.assertIn("250.0000", message)
        self.assertIn("5,000.00", message)
        self.assertIn("20.0000 USDT/IBS", message)
        self.assertIn("池侧", message)
        self.assertIn(f"https://bscscan.com/tx/{trade.transaction_hash}", message)

    def test_large_trade_state_starts_at_current_block_without_history_backfill(self):
        now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        current = make_snapshot(now, 10_000)
        state = monitor.default_state()
        self.assertTrue(monitor.ensure_large_trade_alert_state(state, current))
        self.assertEqual(
            state["large_trade_alerts"]["tracking_started_block"], 10_000
        )
        self.assertEqual(
            state["large_trade_alerts"]["last_scanned_block"], 10_000
        )
        self.assertIsNone(monitor.large_trade_scan_range(state, 10_000))
        self.assertEqual(
            monitor.large_trade_scan_range(state, 10_010),
            (10_001, 10_010),
        )

    def test_large_trade_scan_resumes_from_cursor_and_caps_catch_up(self):
        now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        state = monitor.default_state()
        monitor.ensure_large_trade_alert_state(state, make_snapshot(now, 100))
        state["large_trade_alerts"]["last_scanned_block"] = 150

        with patch.object(monitor, "LARGE_TRADE_MAX_SCAN_BLOCKS", 20):
            self.assertEqual(
                monitor.large_trade_scan_range(state, 1_000),
                (151, 170),
            )

        state["large_trade_alerts"]["last_scanned_block"] = 1_000
        self.assertIsNone(monitor.large_trade_scan_range(state, 1_000))

    def test_large_trade_batches_retry_only_the_failed_batch(self):
        now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        current = make_snapshot(now, 300)
        state = monitor.default_state()
        monitor.ensure_large_trade_alert_state(state, make_snapshot(now, 100))
        trades = [
            monitor.decode_large_trade_log(
                swap_log(
                    0,
                    (5_000 + index) * USDT_SCALE,
                    (250 + index) * IBS_SCALE,
                    0,
                    block=200 + index,
                    log_index=index,
                    tx_byte=f"{index + 1:02x}",
                ),
                True,
            )
            for index in range(7)
        ]
        self.assertTrue(monitor.enqueue_large_trades(state, trades))

        with (
            patch.object(
                monitor,
                "send_telegram",
                side_effect=[None, RuntimeError("second batch failed")],
            ) as send_telegram,
            patch.object(monitor, "atomic_write_json") as atomic_write_json,
        ):
            sent = monitor.send_pending_large_trade_alerts(
                state,
                current,
                "token",
                "chat",
                persist_state=True,
            )

        self.assertEqual(sent, 6)
        self.assertEqual(send_telegram.call_count, 2)
        self.assertEqual(len(state["large_trade_alerts"]["seen_event_ids"]), 6)
        self.assertEqual(len(state["large_trade_alerts"]["pending"]), 1)
        self.assertEqual(
            state["large_trade_alerts"]["pending"][0]["event_id"],
            trades[6].event_id,
        )
        atomic_write_json.assert_called_once()

        with (
            patch.object(monitor, "send_telegram") as retry_send,
            patch.object(monitor, "atomic_write_json") as retry_write,
        ):
            retried = monitor.send_pending_large_trade_alerts(
                state,
                current,
                "token",
                "chat",
            )

        self.assertEqual(retried, 1)
        retry_send.assert_called_once()
        retry_write.assert_called_once()
        self.assertEqual(state["large_trade_alerts"]["pending"], [])
        self.assertEqual(len(state["large_trade_alerts"]["seen_event_ids"]), 7)

    def test_swap_log_timeout_fails_fast_without_recursive_splitting(self):
        web3 = MagicMock()
        web3.eth.get_logs.side_effect = RuntimeError("request timed out")
        rpc_calls = [0]
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            monitor.get_swap_logs_adaptive(web3, 1, 2_000, rpc_calls)
        self.assertEqual(web3.eth.get_logs.call_count, 1)
        self.assertEqual(rpc_calls[0], 1)

    def test_swap_log_deadline_stops_before_another_rpc_call(self):
        web3 = MagicMock()
        with (
            patch.object(monitor.time, "monotonic", return_value=100.0),
            self.assertRaisesRegex(RuntimeError, "时间预算"),
        ):
            monitor.get_swap_logs_adaptive(web3, 1, 2_000, [0], deadline=99.0)
        web3.eth.get_logs.assert_not_called()

    def test_swap_log_range_limit_is_split_adaptively(self):
        web3 = MagicMock()

        def get_logs(params):
            if params["toBlock"] - params["fromBlock"] + 1 > 10:
                raise RuntimeError("-32005 limit exceeded")
            return []

        web3.eth.get_logs.side_effect = get_logs
        rpc_calls = [0]
        logs = monitor.get_swap_logs_adaptive(web3, 1, 20, rpc_calls)
        self.assertEqual(logs, [])
        self.assertEqual(web3.eth.get_logs.call_count, 3)
        self.assertEqual(rpc_calls[0], 3)

    def test_safety_only_change_can_trigger_immediate_treasury_report(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        previous = make_snapshot(now - timedelta(minutes=5), 100, safety=200_000)
        current = make_snapshot(now, 200, safety=350_000)
        previous_record = monitor.record_from_snapshot(
            previous, monitor.FlowSummary(100, 100, False)
        )
        flow = monitor.summarize_interval(previous_record, current)
        with patch.object(
            monitor, "IMMEDIATE_TOTAL_CHANGE_USDT", monitor.Decimal("100000")
        ):
            due, reason = monitor.report_due({"latest": previous_record}, current, flow)
        self.assertTrue(due)
        self.assertEqual(reason, "IMMEDIATE_TREASURY_CHANGE")

    def test_telegram_message_contains_new_and_existing_metrics(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        snapshot = make_snapshot(now, 200)
        baseline = monitor.build_telegram_message(
            snapshot,
            None,
            None,
            monitor.FlowSummary(200, 200, False),
            "🔵 建立基线",
            ["等待至少24小时数据"],
            None,
            "BASELINE",
        )
        self.assertIn("当前国库资金", baseline)
        self.assertIn("Safety", baseline)
        self.assertIn("流通市值估算", baseline)
        self.assertIn("当前总量", baseline)
        self.assertIn("可见USDT覆盖", baseline)

        metrics_24h = make_metrics(
            -50_000,
            1,
            total_change=-0.01,
            backing_change=-0.01,
            external=-25_000 * USDT_SCALE,
            treasury_delta=-200_000,
            supply_delta=1_000,
        )
        pressure = monitor.SellPressure(
            101, 200, 2_000 * IBS_SCALE, 40_000 * USDT_SCALE, 12
        )
        message = monitor.build_telegram_message(
            snapshot,
            metrics_24h,
            None,
            monitor.FlowSummary(101, 200, True),
            "🟠 下行阶段",
            ["国库资金下降", "外部净流出"],
            monitor.Decimal("30"),
            "PERIODIC",
            treasury_runway_days=monitor.Decimal("60"),
            sell_pressure=pressure,
        )
        self.assertIn("国库静态可持续时间：约60.0天", message)
        self.assertIn("近24h净增发 +1,000.00 IBS", message)
        self.assertIn("近24h真实抛压 40,000.00 USDT", message)
        self.assertIn("LP+RBS 24h", message)
        self.assertIn("已知协议地址净流入", message)

    def test_24h_window_tolerance_does_not_label_22h_as_a_day(self):
        end = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        too_short = make_snapshot(end - timedelta(hours=22), 100)
        near_day = make_snapshot(end - timedelta(hours=23, minutes=30), 110)
        current = make_snapshot(end, 200)
        too_short_record = monitor.record_from_snapshot(
            too_short, monitor.FlowSummary(100, 100, False)
        )
        near_day_record = monitor.record_from_snapshot(
            near_day, monitor.FlowSummary(110, 110, False)
        )
        current_record = monitor.record_from_snapshot(
            current, monitor.FlowSummary(111, 200, True)
        )
        self.assertIsNone(
            monitor.calculate_window(
                [too_short_record, current_record],
                current_record,
                "24h",
                timedelta(hours=24),
            )
        )
        metrics = monitor.calculate_window(
            [too_short_record, near_day_record, current_record],
            current_record,
            "24h",
            timedelta(hours=24),
        )
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics.start_block, 110)

    def test_growth_and_death_spiral_classification(self):
        growth_24 = make_metrics(15_000, 1, 0.01, 0.01, 1)
        growth_7 = make_metrics(45_000, 7, 0.05, 0.02, 1)
        self.assertEqual(
            monitor.classify_health(growth_24, growth_7, None)[0], "GROWTH"
        )

        bad_24 = make_metrics(-75_000, 1, -0.03, -0.02, -1)
        bad_7 = make_metrics(-300_000, 7, -0.12, -0.08, -1)
        self.assertEqual(
            monitor.classify_health(bad_24, bad_7, monitor.Decimal("6"))[0],
            "DEATH_SPIRAL_RISK",
        )

    def test_first_run_writes_new_state_without_sending_real_telegram(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        snapshot = make_snapshot(now, 200)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "data" / "usdt_balance.json"
            history_path = Path(temp_dir) / "data" / "usdt_flow_history.csv"
            with (
                patch.object(monitor, "STATE_FILE", state_path),
                patch.object(monitor, "HISTORY_FILE", history_path),
                patch.object(monitor, "connect_web3", return_value=object()),
                patch.object(monitor, "read_current_snapshot", return_value=snapshot),
                patch.object(monitor, "send_telegram") as send_telegram,
                patch.dict(
                    monitor.os.environ,
                    {"BSC_RPC": "https://example.invalid", "BOT_TOKEN": "x", "CHAT_ID": "y"},
                ),
            ):
                monitor.main()

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], monitor.STATE_SCHEMA_VERSION)
            self.assertEqual(saved["latest"]["protocol_usdt_raw"], str(snapshot.protocol_usdt_raw))
            self.assertTrue(history_path.exists())
            send_telegram.assert_called_once()

    def test_non_report_run_sends_large_trade_without_moving_funds_baseline(self):
        now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        previous = make_snapshot(now - timedelta(minutes=5), 100)
        current = make_snapshot(now, 200)
        previous_record = monitor.record_from_snapshot(
            previous, monitor.FlowSummary(100, 100, False)
        )
        state = monitor.default_state()
        state["latest"] = previous_record
        state["snapshots"] = [previous_record]
        state["large_trade_alerts"] = {
            "tracking_started_at_utc": previous.observed_at.isoformat(),
            "tracking_started_block": 100,
            "seen_event_ids": [],
            "pending": [],
            "last_alert_at_utc": None,
        }
        trade = monitor.decode_large_trade_log(
            swap_log(
                0,
                5_000 * USDT_SCALE,
                250 * IBS_SCALE,
                0,
                block=150,
                log_index=8,
                tx_byte="cd",
            ),
            True,
        )
        with (
            patch.object(monitor, "load_state", return_value=state),
            patch.object(monitor, "connect_web3", return_value=object()),
            patch.object(monitor, "read_current_snapshot", return_value=current),
            patch.object(
                monitor, "read_large_trades", return_value=[trade]
            ) as read_large_trades,
            patch.object(monitor, "send_telegram") as send_telegram,
            patch.object(monitor, "atomic_write_json") as atomic_write_json,
            patch.object(monitor, "update_state") as update_state,
            patch.dict(
                monitor.os.environ,
                {
                    "BSC_RPC": "https://example.invalid",
                    "BOT_TOKEN": "x",
                    "CHAT_ID": "y",
                },
            ),
        ):
            monitor.main()

        read_large_trades.assert_called_once()
        send_telegram.assert_called_once()
        self.assertIn("大额买入", send_telegram.call_args.args[2])
        update_state.assert_not_called()
        self.assertEqual(state["latest"]["block_number"], 100)
        self.assertEqual(state["large_trade_alerts"]["pending"], [])
        self.assertIn(
            trade.event_id,
            state["large_trade_alerts"]["seen_event_ids"],
        )
        atomic_write_json.assert_called_once()

    def test_large_trade_empty_catch_up_persists_and_advances_each_run(self):
        now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        previous = make_snapshot(now - timedelta(minutes=5), 100)
        current = make_snapshot(now, 1_000)
        previous_record = monitor.record_from_snapshot(
            previous, monitor.FlowSummary(100, 100, False)
        )
        state = monitor.default_state()
        state["latest"] = previous_record
        state["snapshots"] = [previous_record]
        state["large_trade_alerts"] = {
            "tracking_started_at_utc": previous.observed_at.isoformat(),
            "tracking_started_block": 100,
            "last_scanned_block": 100,
            "seen_event_ids": [],
            "pending": [],
            "last_alert_at_utc": None,
        }
        with (
            patch.object(monitor, "load_state", return_value=state),
            patch.object(monitor, "connect_web3", return_value=object()),
            patch.object(monitor, "read_current_snapshot", return_value=current),
            patch.object(
                monitor, "read_large_trades", return_value=[]
            ) as read_large_trades,
            patch.object(monitor, "send_telegram") as send_telegram,
            patch.object(monitor, "atomic_write_json") as atomic_write_json,
            patch.object(monitor, "update_state") as update_state,
            patch.object(monitor, "LARGE_TRADE_MAX_SCAN_BLOCKS", 20),
            patch.dict(
                monitor.os.environ,
                {
                    "BSC_RPC": "https://example.invalid",
                    "BOT_TOKEN": "x",
                    "CHAT_ID": "y",
                },
            ),
        ):
            monitor.main()
            monitor.main()

        self.assertEqual(
            [(call.args[1], call.args[2]) for call in read_large_trades.call_args_list],
            [(101, 120), (121, 140)],
        )
        self.assertEqual(state["large_trade_alerts"]["last_scanned_block"], 140)
        self.assertEqual(atomic_write_json.call_count, 2)
        send_telegram.assert_not_called()
        update_state.assert_not_called()

    def test_failed_large_trade_telegram_stays_pending_for_retry(self):
        now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        previous = make_snapshot(now - timedelta(minutes=5), 100)
        current = make_snapshot(now, 200)
        previous_record = monitor.record_from_snapshot(
            previous, monitor.FlowSummary(100, 100, False)
        )
        state = monitor.default_state()
        state["latest"] = previous_record
        state["snapshots"] = [previous_record]
        state["large_trade_alerts"] = {
            "tracking_started_at_utc": previous.observed_at.isoformat(),
            "tracking_started_block": 100,
            "seen_event_ids": [],
            "pending": [],
            "last_alert_at_utc": None,
        }
        trade = monitor.decode_large_trade_log(
            swap_log(
                250 * IBS_SCALE,
                0,
                0,
                5_000 * USDT_SCALE,
                block=151,
                log_index=9,
                tx_byte="ef",
            ),
            True,
        )
        with (
            patch.object(monitor, "load_state", return_value=state),
            patch.object(monitor, "connect_web3", return_value=object()),
            patch.object(monitor, "read_current_snapshot", return_value=current),
            patch.object(monitor, "read_large_trades", return_value=[trade]),
            patch.object(
                monitor,
                "send_telegram",
                side_effect=RuntimeError("telegram down"),
            ),
            patch.object(monitor, "atomic_write_json") as atomic_write_json,
            patch.dict(
                monitor.os.environ,
                {
                    "BSC_RPC": "https://example.invalid",
                    "BOT_TOKEN": "x",
                    "CHAT_ID": "y",
                },
            ),
        ):
            monitor.main()

        self.assertEqual(
            state["large_trade_alerts"]["pending"][0]["event_id"],
            trade.event_id,
        )
        self.assertEqual(state["large_trade_alerts"]["seen_event_ids"], [])
        atomic_write_json.assert_called_once()

    def test_sell_pressure_rpc_failure_does_not_block_telegram_report(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        start = make_snapshot(now - timedelta(hours=24), 100)
        current = make_snapshot(now, 200)
        start_record = monitor.record_from_snapshot(
            start, monitor.FlowSummary(100, 100, False)
        )
        state = {
            "latest": start_record,
            "snapshots": [start_record],
        }
        with (
            patch.object(monitor, "load_state", return_value=state),
            patch.object(monitor, "connect_web3", return_value=object()),
            patch.object(monitor, "read_current_snapshot", return_value=current),
            patch.object(
                monitor,
                "read_sell_pressure",
                side_effect=RuntimeError("request timed out"),
            ) as read_sell_pressure,
            patch.object(monitor, "send_telegram") as send_telegram,
            patch.object(monitor, "append_history"),
            patch.object(monitor, "update_state") as update_state,
            patch.dict(
                monitor.os.environ,
                {"BSC_RPC": "https://example.invalid", "BOT_TOKEN": "x", "CHAT_ID": "y"},
            ),
        ):
            monitor.main()

        read_sell_pressure.assert_called_once()
        send_telegram.assert_called_once()
        self.assertIn("暂时无法读取", send_telegram.call_args.args[2])
        saved_record = update_state.call_args.args[1]
        self.assertIsNone(saved_record["report_metrics"]["sell_pressure_24h"])


if __name__ == "__main__":
    unittest.main()
