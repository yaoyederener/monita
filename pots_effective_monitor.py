#!/usr/bin/env python3
"""POTS solvency-first monitor using only visible USDT assets.

Every run compares the current confirmed block with the immediately previous
observation. It intentionally avoids 24h/7d windows and ordinary trade alerts.
"""

from __future__ import annotations

import html
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import requests

from usdt_balance_monitor import (
    CurrentSnapshot,
    connect_web3,
    fmt_amount,
    fmt_signed_amount,
    read_current_snapshot,
    token_amount,
)


STATE_FILE = Path(os.getenv("POTS_EFFECTIVE_STATE_FILE", "data/pots_effective_state.json"))
TELEGRAM_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "20"))
AMM_FEE_MULTIPLIER = Decimal(os.getenv("AMM_FEE_MULTIPLIER", "0.9975"))
REPORT_INTERVAL_MINUTES = int(os.getenv("REPORT_INTERVAL_MINUTES", "60"))
IMMEDIATE_SUPPLY_CHANGE_IBS = Decimal(os.getenv("IMMEDIATE_SUPPLY_CHANGE_IBS", "1"))
SHORT_TREND_MINUTES = int(os.getenv("SHORT_TREND_MINUTES", "30"))
SHORT_TREND_SLICES = int(os.getenv("SHORT_TREND_SLICES", "6"))
WARNING_LP_DROP_PCT = Decimal(os.getenv("WARNING_LP_DROP_PCT", "1"))
CRITICAL_LP_DROP_PCT = Decimal(os.getenv("CRITICAL_LP_DROP_PCT", "3"))
LIQUIDITY_REMOVAL_PCT = Decimal(os.getenv("LIQUIDITY_REMOVAL_PCT", "1"))
CRITICAL_BACKUP_DROP_USDT = Decimal(os.getenv("CRITICAL_BACKUP_DROP_USDT", "100000"))


@dataclass(frozen=True)
class IntervalChange:
    elapsed_seconds: int
    lp_delta_raw: int
    rbs_delta_raw: int
    safety_delta_raw: int
    treasury_delta_raw: int
    protocol_delta_raw: Optional[int]
    supply_delta_raw: int
    circulating_delta_raw: int
    lp_ibs_delta_raw: int


@dataclass(frozen=True)
class SolvencyMetrics:
    external_circulating_ibs: Decimal
    spot_liability_usdt: Decimal
    visible_backing_price: Decimal
    spot_coverage_ratio: Decimal
    fully_covered_ibs: Decimal
    fully_covered_ratio: Decimal
    spot_funding_gap_usdt: Decimal
    lp_full_exit_usdt: Decimal
    lp_full_exit_price: Decimal


@dataclass(frozen=True)
class SimpleSnapshot:
    timestamp_utc: datetime
    block_number: int
    lp_usdt_raw: int
    lp_ibs_raw: int
    rbs_usdt_raw: int
    safety_usdt_raw: int
    treasury_usdt_raw: int
    ibs_total_supply_raw: int


@dataclass(frozen=True)
class ShortTrend:
    elapsed_seconds: int
    sample_intervals: int
    lp_decrease_intervals: int
    lp_delta_raw: int
    lp_delta_pct: Decimal
    lp_ibs_delta_raw: int
    rbs_delta_raw: int
    safety_delta_raw: int
    treasury_delta_raw: int
    supply_delta_raw: int


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"schema_version": 2, "latest": None, "observations": [], "last_report_at_utc": None}
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    state["schema_version"] = 2
    state.setdefault("latest", None)
    state.setdefault("observations", [])
    state.setdefault("last_report_at_utc", None)
    if not state["observations"] and state["latest"]:
        state["observations"] = [state["latest"]]
    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    temp.replace(STATE_FILE)


def snapshot_record(current: CurrentSnapshot) -> dict[str, Any]:
    return {
        "timestamp_utc": current.observed_at.isoformat(),
        "block_number": current.block_number,
        "usdt_decimals": current.usdt_decimals,
        "ibs_decimals": current.ibs_decimals,
        "lp_usdt_raw": str(current.lp_usdt_raw),
        "lp_ibs_raw": str(current.lp_ibs_raw),
        "rbs_usdt_raw": str(current.rbs_usdt_raw),
        "safety_usdt_raw": str(current.safety_usdt_raw),
        "treasury_usdt_raw": str(current.treasury_usdt_raw),
        "protocol_usdt_raw": str(current.protocol_usdt_raw),
        "protocol_config_hash": current.protocol_config_hash,
        "ibs_total_supply_raw": str(current.ibs_total_supply_raw),
        "ibs_circulating_raw": str(current.ibs_circulating_raw),
        "ibs_price_usdt": str(current.ibs_price_usdt),
        "backing_per_ibs": str(current.backing_per_ibs),
    }


def simple_snapshot(record: dict[str, Any]) -> Optional[SimpleSnapshot]:
    observed_at = parse_time(record.get("timestamp_utc"))
    required = (
        "block_number", "lp_usdt_raw", "lp_ibs_raw", "rbs_usdt_raw",
        "safety_usdt_raw", "treasury_usdt_raw", "ibs_total_supply_raw",
    )
    if observed_at is None or any(record.get(key) is None for key in required):
        return None
    return SimpleSnapshot(
        timestamp_utc=observed_at,
        block_number=int(record["block_number"]),
        lp_usdt_raw=int(record["lp_usdt_raw"]),
        lp_ibs_raw=int(record["lp_ibs_raw"]),
        rbs_usdt_raw=int(record["rbs_usdt_raw"]),
        safety_usdt_raw=int(record["safety_usdt_raw"]),
        treasury_usdt_raw=int(record["treasury_usdt_raw"]),
        ibs_total_supply_raw=int(record["ibs_total_supply_raw"]),
    )


def append_observation(state: dict[str, Any], current: CurrentSnapshot) -> None:
    record = snapshot_record(current)
    observations = [
        item for item in state.get("observations", [])
        if int(item.get("block_number", -1)) != current.block_number
    ]
    observations.append(record)
    cutoff = current.observed_at.timestamp() - 48 * 3600
    state["observations"] = [
        item for item in observations
        if (parse_time(item.get("timestamp_utc")) or current.observed_at).timestamp() >= cutoff
    ][-600:]
    state["latest"] = record
    state["updated_at_utc"] = now_utc().isoformat()


def calculate_short_trend(
    records: list[dict[str, Any]], current: CurrentSnapshot
) -> Optional[ShortTrend]:
    current_record = snapshot_record(current)
    snapshots = [simple_snapshot(item) for item in records]
    valid = [item for item in snapshots if item is not None and item.block_number < current.block_number]
    cutoff = current.observed_at.timestamp() - SHORT_TREND_MINUTES * 60
    recent = [item for item in valid if item.timestamp_utc.timestamp() >= cutoff]
    if not valid:
        return None
    # Prefer the configured short window when it contains enough samples to
    # judge persistence. External schedulers can run only once per hour; in
    # that case a strict 30-minute filter would discard every prior sample and
    # leave the trend in "accumulating" forever. Fall back to the latest
    # observations and report their real elapsed span in the message.
    minimum_samples = min(4, len(valid))
    if len(recent) < minimum_samples:
        recent = valid[-SHORT_TREND_SLICES:]
    current_simple = simple_snapshot(current_record)
    assert current_simple is not None
    series = recent[-SHORT_TREND_SLICES:] + [current_simple]
    if len(series) < 2:
        return None
    start = series[0]
    lp_drop_count = sum(
        1 for previous, following in zip(series, series[1:])
        if following.lp_usdt_raw < previous.lp_usdt_raw
    )
    lp_delta = current_simple.lp_usdt_raw - start.lp_usdt_raw
    lp_pct = (
        Decimal(lp_delta) / Decimal(start.lp_usdt_raw) * 100
        if start.lp_usdt_raw > 0 else Decimal(0)
    )
    return ShortTrend(
        elapsed_seconds=max(1, int((current_simple.timestamp_utc - start.timestamp_utc).total_seconds())),
        sample_intervals=len(series) - 1,
        lp_decrease_intervals=lp_drop_count,
        lp_delta_raw=lp_delta,
        lp_delta_pct=lp_pct,
        lp_ibs_delta_raw=current_simple.lp_ibs_raw - start.lp_ibs_raw,
        rbs_delta_raw=current_simple.rbs_usdt_raw - start.rbs_usdt_raw,
        safety_delta_raw=current_simple.safety_usdt_raw - start.safety_usdt_raw,
        treasury_delta_raw=current_simple.treasury_usdt_raw - start.treasury_usdt_raw,
        supply_delta_raw=current_simple.ibs_total_supply_raw - start.ibs_total_supply_raw,
    )


def interval_change(previous: Optional[dict[str, Any]], current: CurrentSnapshot) -> Optional[IntervalChange]:
    if previous is None:
        return None
    previous_time = parse_time(previous.get("timestamp_utc"))
    if previous_time is None:
        return None
    if current.block_number < int(previous.get("block_number", 0)):
        raise RuntimeError("当前确认区块早于上次观察区块，本次不覆盖状态")
    elapsed_seconds = max(1, int((current.observed_at - previous_time).total_seconds()))
    same_boundary = previous.get("protocol_config_hash") == current.protocol_config_hash
    protocol_delta = (
        current.protocol_usdt_raw - int(previous["protocol_usdt_raw"])
        if same_boundary and previous.get("protocol_usdt_raw") is not None else None
    )
    return IntervalChange(
        elapsed_seconds=elapsed_seconds,
        lp_delta_raw=current.lp_usdt_raw - int(previous["lp_usdt_raw"]),
        rbs_delta_raw=current.rbs_usdt_raw - int(previous["rbs_usdt_raw"]),
        safety_delta_raw=current.safety_usdt_raw - int(previous["safety_usdt_raw"]),
        treasury_delta_raw=current.treasury_usdt_raw - int(previous["treasury_usdt_raw"]),
        protocol_delta_raw=protocol_delta,
        supply_delta_raw=current.ibs_total_supply_raw - int(previous["ibs_total_supply_raw"]),
        circulating_delta_raw=current.ibs_circulating_raw - int(previous["ibs_circulating_raw"]),
        # Schema v1 did not store the LP IBS side. The first run after upgrade
        # cannot classify a one-interval liquidity removal, so use the current
        # reserve as its migration baseline and start measuring from this run.
        lp_ibs_delta_raw=current.lp_ibs_raw - int(previous.get("lp_ibs_raw", current.lp_ibs_raw)),
    )


def calculate_solvency(current: CurrentSnapshot) -> SolvencyMetrics:
    circulating = token_amount(current.ibs_circulating_raw, current.ibs_decimals)
    lp_ibs = token_amount(current.lp_ibs_raw, current.ibs_decimals)
    # IBS already held by the LP is inventory, not an external holder's sellable claim.
    external_circulating = max(Decimal(0), circulating - lp_ibs)
    treasury = token_amount(current.treasury_usdt_raw, current.usdt_decimals)
    spot_liability = external_circulating * current.ibs_price_usdt
    backing_price = treasury / circulating if circulating > 0 else Decimal(0)
    coverage = treasury / spot_liability if spot_liability > 0 else Decimal(0)
    covered_ibs = treasury / current.ibs_price_usdt if current.ibs_price_usdt > 0 else Decimal(0)
    covered_ratio = min(Decimal(1), covered_ibs / external_circulating) if external_circulating > 0 else Decimal(0)
    gap = max(Decimal(0), spot_liability - treasury)

    lp_usdt = token_amount(current.lp_usdt_raw, current.usdt_decimals)
    effective_input = external_circulating * AMM_FEE_MULTIPLIER
    full_exit_usdt = (
        lp_usdt * effective_input / (lp_ibs + effective_input)
        if lp_ibs + effective_input > 0 else Decimal(0)
    )
    full_exit_price = full_exit_usdt / external_circulating if external_circulating > 0 else Decimal(0)
    return SolvencyMetrics(
        external_circulating_ibs=external_circulating,
        spot_liability_usdt=spot_liability,
        visible_backing_price=backing_price,
        spot_coverage_ratio=coverage,
        fully_covered_ibs=covered_ibs,
        fully_covered_ratio=covered_ratio,
        spot_funding_gap_usdt=gap,
        lp_full_exit_usdt=full_exit_usdt,
        lp_full_exit_price=full_exit_price,
    )


def report_due(
    previous: Optional[dict[str, Any]],
    current: CurrentSnapshot,
    change: Optional[IntervalChange],
    trend: Optional[ShortTrend] = None,
    last_report_at: Optional[datetime] = None,
) -> tuple[bool, str]:
    if previous is None or change is None:
        return True, "建立基线"
    backup_threshold = int(CRITICAL_BACKUP_DROP_USDT * (Decimal(10) ** current.usdt_decimals))
    if change.rbs_delta_raw <= -backup_threshold:
        return True, "RBS备用金大额减少"
    if change.safety_delta_raw <= -backup_threshold:
        return True, "Safety备用金大额减少"
    if interval_liquidity_removal_suspected(change, current):
        return True, "疑似撤出流动性"
    if liquidity_removal_suspected(trend, current):
        return True, "疑似撤出流动性"
    if trend is not None and trend.lp_delta_pct <= -CRITICAL_LP_DROP_PCT:
        return True, "LP短期大幅减少"
    supply_threshold_raw = int(IMMEDIATE_SUPPLY_CHANGE_IBS * (Decimal(10) ** current.ibs_decimals))
    if change.supply_delta_raw >= supply_threshold_raw and change.lp_delta_raw < 0:
        return True, "IBS增发同时LP资金流出"
    # Older state files and the first run of this monitor have no successful
    # Telegram report timestamp. Send once immediately to establish that
    # baseline; using the observation interval here would reset the clock on
    # every frequent check and could prevent the hourly report forever.
    if last_report_at is None:
        return True, "建立通知基线"
    interval_seconds = REPORT_INTERVAL_MINUTES * 60
    current_window = int(current.observed_at.timestamp()) // interval_seconds
    last_report_window = int(last_report_at.timestamp()) // interval_seconds
    if current_window > last_report_window:
        return True, "定时报告"
    return False, "尚未到报告时间且没有重要变化"


def daily_outflow_raw(delta_raw: int, elapsed_seconds: int) -> Optional[Decimal]:
    if delta_raw >= 0 or elapsed_seconds <= 0:
        return None
    return Decimal(-delta_raw) * Decimal(86400) / Decimal(elapsed_seconds)


def runway_days(current_raw: int, delta_raw: int, elapsed_seconds: int) -> Optional[Decimal]:
    daily = daily_outflow_raw(delta_raw, elapsed_seconds)
    if daily is None or daily <= 0:
        return None
    return Decimal(current_raw) / daily


def fmt_interval(seconds: int) -> str:
    minutes = Decimal(seconds) / Decimal(60)
    return f"{minutes:.1f}分钟" if minutes < 120 else f"{minutes / 60:.1f}小时"


def fmt_runway(days: Optional[Decimal], delta_raw: int) -> str:
    if delta_raw >= 0:
        return "本次未减少"
    if days is None:
        return "无法估算"
    if days > 999:
        return ">999天"
    return f"约{days:.1f}天"


def fmt_change(delta_raw: int, decimals: int) -> str:
    icon = "增加" if delta_raw > 0 else "减少" if delta_raw < 0 else "不变"
    return f"{icon} {fmt_amount(abs(delta_raw), decimals)}"


def liquidity_removal_suspected(trend: Optional[ShortTrend], current: CurrentSnapshot) -> bool:
    if trend is None or trend.lp_delta_raw >= 0 or trend.lp_ibs_delta_raw >= 0:
        return False
    start_usdt = current.lp_usdt_raw - trend.lp_delta_raw
    start_ibs = current.lp_ibs_raw - trend.lp_ibs_delta_raw
    if start_usdt <= 0 or start_ibs <= 0:
        return False
    usdt_drop_pct = Decimal(-trend.lp_delta_raw) / Decimal(start_usdt) * 100
    ibs_drop_pct = Decimal(-trend.lp_ibs_delta_raw) / Decimal(start_ibs) * 100
    if usdt_drop_pct < LIQUIDITY_REMOVAL_PCT or ibs_drop_pct < LIQUIDITY_REMOVAL_PCT:
        return False
    ratio = usdt_drop_pct / ibs_drop_pct
    return Decimal("0.75") <= ratio <= Decimal("1.25")


def interval_liquidity_removal_suspected(change: IntervalChange, current: CurrentSnapshot) -> bool:
    if change.lp_delta_raw >= 0 or change.lp_ibs_delta_raw >= 0:
        return False
    start_usdt = current.lp_usdt_raw - change.lp_delta_raw
    start_ibs = current.lp_ibs_raw - change.lp_ibs_delta_raw
    if start_usdt <= 0 or start_ibs <= 0:
        return False
    usdt_drop_pct = Decimal(-change.lp_delta_raw) / Decimal(start_usdt) * 100
    ibs_drop_pct = Decimal(-change.lp_ibs_delta_raw) / Decimal(start_ibs) * 100
    if usdt_drop_pct < LIQUIDITY_REMOVAL_PCT or ibs_drop_pct < LIQUIDITY_REMOVAL_PCT:
        return False
    ratio = usdt_drop_pct / ibs_drop_pct
    return Decimal("0.75") <= ratio <= Decimal("1.25")


def classify_risk(
    current: CurrentSnapshot,
    change: Optional[IntervalChange],
    trend: Optional[ShortTrend],
) -> tuple[str, list[str]]:
    if change is None or trend is None:
        return "🔵 建立资金基线", ["正在积累约30分钟趋势数据"]
    reasons: list[str] = []
    persistent = trend.sample_intervals >= 4 and trend.lp_decrease_intervals * 3 >= trend.sample_intervals * 2
    lp_outflow = trend.lp_delta_raw < 0
    backup_drop = trend.rbs_delta_raw < 0 or trend.safety_delta_raw < 0
    supply_cashout = trend.supply_delta_raw > 0 and lp_outflow
    removal = liquidity_removal_suspected(trend, current)
    runway = runway_days(current.treasury_usdt_raw, trend.treasury_delta_raw, trend.elapsed_seconds)
    backup_threshold = int(CRITICAL_BACKUP_DROP_USDT * (Decimal(10) ** current.usdt_decimals))
    critical_backup = trend.rbs_delta_raw <= -backup_threshold or trend.safety_delta_raw <= -backup_threshold
    if lp_outflow:
        reasons.append(f"短期LP资金净流出{abs(trend.lp_delta_pct):.2f}%")
    if persistent:
        reasons.append(f"最近{trend.sample_intervals}次检查有{trend.lp_decrease_intervals}次下降")
    if backup_drop:
        reasons.append("备用资金正在减少")
    if supply_cashout:
        reasons.append("IBS增发同时LP资金减少")
    if removal:
        reasons.append("LP两侧储备同比例下降，疑似撤出流动性")
    if runway is not None and runway <= 7 and persistent:
        reasons.append("按稳定短期流速可支撑不足7天")
    if removal or critical_backup or trend.lp_delta_pct <= -CRITICAL_LP_DROP_PCT or (
        supply_cashout and persistent
    ) or (runway is not None and runway <= 7 and persistent):
        return "🔴 高风险", reasons
    if (persistent and trend.lp_delta_pct <= -WARNING_LP_DROP_PCT) or backup_drop or supply_cashout:
        return "🟠 资金承压", reasons
    if trend.treasury_delta_raw > 0 and not backup_drop:
        return "🟢 资金改善", ["短期全部可见USDT增加"]
    return "🟡 暂时稳定", reasons or ["短期USDT没有持续异常流出"]


def build_message(
    current: CurrentSnapshot,
    change: Optional[IntervalChange],
    trend: Optional[ShortTrend] = None,
) -> str:
    risk, reasons = classify_risk(current, change, trend)
    udec = current.usdt_decimals
    if change is None:
        delta_lp = delta_rbs = delta_safety = delta_treasury = "等待下一次"
        interval = "基线"
    else:
        interval = fmt_interval(change.elapsed_seconds)
        delta_lp = fmt_change(change.lp_delta_raw, udec)
        delta_rbs = fmt_change(change.rbs_delta_raw, udec)
        delta_safety = fmt_change(change.safety_delta_raw, udec)
        delta_treasury = fmt_change(change.treasury_delta_raw, udec)

    reasons_text = "；".join(html.escape(x) for x in reasons)
    if trend is None:
        trend_interval = "积累中"
        trend_change = "等待约30分钟数据"
        trend_frequency = "积累中"
        burn_rate = "暂不计算"
        runway_text = "暂不计算"
        cause = "正在积累趋势数据"
    else:
        trend_interval = fmt_interval(trend.elapsed_seconds)
        trend_change = f"{fmt_change(trend.treasury_delta_raw, udec)} USDT"
        trend_frequency = f"{trend.lp_decrease_intervals}/{trend.sample_intervals}次下降"
        persistent = trend.sample_intervals >= 4 and trend.lp_decrease_intervals * 3 >= trend.sample_intervals * 2
        daily = daily_outflow_raw(trend.treasury_delta_raw, trend.elapsed_seconds) if persistent else None
        runway = runway_days(current.treasury_usdt_raw, trend.treasury_delta_raw, trend.elapsed_seconds) if persistent else None
        burn_rate = f"约 {token_amount(int(daily), udec):,.2f} USDT/天" if daily is not None else "资金有涨有跌，暂不外推"
        runway_text = fmt_runway(runway, trend.treasury_delta_raw) if persistent else "暂不计算"
        if liquidity_removal_suspected(trend, current):
            cause = "疑似撤出流动性（LP两侧储备同步下降）"
        elif trend.supply_delta_raw > 0 and trend.lp_delta_raw < 0:
            cause = "IBS增发期间LP资金流出，需核查是否套现"
        elif trend.lp_delta_raw < 0 and trend.lp_ibs_delta_raw > 0:
            cause = "以用户卖出IBS造成的市场抛压为主"
        elif trend.lp_delta_raw > 0 and trend.lp_ibs_delta_raw < 0:
            cause = "以用户买入IBS带来的资金流入为主"
        elif trend.rbs_delta_raw < 0 or trend.safety_delta_raw < 0:
            backup_drop = -(min(0, trend.rbs_delta_raw) + min(0, trend.safety_delta_raw))
            if trend.lp_delta_raw > 0 and Decimal(trend.lp_delta_raw) >= Decimal(backup_drop) * Decimal("0.8"):
                cause = "备用资金减少、LP同步增加，疑似内部补充流动性"
            else:
                cause = "备用资金减少且未进入LP，属于真实资金外流"
        else:
            cause = "未发现明确异常资金动作"
    lines = [
        f"<b>{risk}｜POTS资金安全监控</b>",
        f"判断：{reasons_text}",
        "",
        "💵 <b>1. 可以退出的资金</b>",
        f"LP：<b>{fmt_amount(current.lp_usdt_raw, udec)} USDT</b>",
        f"距上次检查（{interval}）：{delta_lp} USDT",
        "",
        "🏦 <b>2. 备用资金</b>",
        f"RBS：{fmt_amount(current.rbs_usdt_raw, udec)} USDT ｜ {delta_rbs} USDT",
        f"Safety：{fmt_amount(current.safety_usdt_raw, udec)} USDT ｜ {delta_safety} USDT",
        "",
        "💰 <b>3. 全部可见USDT</b>",
        f"合计：<b>{fmt_amount(current.treasury_usdt_raw, udec)} USDT</b>",
        f"距上次检查：{delta_treasury} USDT",
        "",
        f"📉 <b>4. 短期趋势（{trend_interval}）</b>",
        f"全部资金：{trend_change} ｜ LP：{trend_frequency}",
        f"减少速度：{burn_rate}",
        f"按稳定短期速度可支撑：<b>{runway_text}</b>",
        f"主要原因：{cause}",
        "",
        "说明：这里只统计LP、RBS、Safety里的USDT，不把IBS当资产。支撑天数只在资金连续下降时估算，用于预警，不是保证。",
        f"确认区块：<code>{current.block_number}</code>",
    ]
    if current.lp_balance_usdt_raw != current.lp_usdt_raw:
        lines.append(f"⚠️ LP代币余额与储备相差 {fmt_signed_amount(current.lp_balance_usdt_raw - current.lp_usdt_raw, udec)} USDT")
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, message: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=TELEGRAM_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError(f"Telegram发送失败：{response.text}")


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


def main() -> None:
    rpc_url = require_env("BSC_RPC")
    token = require_env("BOT_TOKEN")
    chat_id = require_env("CHAT_ID")
    current = read_current_snapshot(connect_web3(rpc_url))
    state = load_state()
    previous = state.get("latest")
    change = interval_change(previous, current)
    trend = calculate_short_trend(state.get("observations", []), current)
    due, trigger = report_due(
        previous,
        current,
        change,
        trend,
        parse_time(state.get("last_report_at_utc")),
    )
    append_observation(state, current)
    if not due:
        save_state(state)
        print(f"有效资金监控：{trigger}；区块{current.block_number}")
        return
    send_telegram(token, chat_id, build_message(current, change, trend))
    state["last_report_at_utc"] = current.observed_at.isoformat()
    save_state(state)
    print(f"有效资金监控成功：区块{current.block_number}，触发={trigger}，变化区间={'基线' if change is None else fmt_interval(change.elapsed_seconds)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"POTS有效资金监控失败：{exc}", flush=True)
        raise
