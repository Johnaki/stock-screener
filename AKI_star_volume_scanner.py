from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests


DEFAULT_CONFIG: dict[str, Any] = {
    "market": {
        "adjust": "qfq",
        "history_days": 260,
        "max_workers": 6,
        "request_delay_seconds": 0.15,
        "exclude_st": True,
        "exclude_beijing": True,
    },
    "filters": {
        "min_price": 3,
        "max_price": 80,
        "min_total_market_cap_yi": 20,
        "max_total_market_cap_yi": 800,
        "top_n": 30,
    },
    "star_volume": {
        "lookback_days": 12,
        "volume_shrink_ratio": 0.55,
        "tight_price_range": 0.16,
        "near_low_ratio": 0.28,
        "max_recent_gain_20d": 0.22,
        "min_setup_days": 4,
    },
    "breakout": {
        "confirm_window_days": 20,
        "min_gain_from_setup_low": 0.25,
        "min_recent_gain_5d": 0.12,
        "volume_burst_ratio": 1.8,
    },
    "push": {
        "enabled": True,
    },
}


@dataclasses.dataclass(slots=True)
class StockMeta:
    code: str
    name: str
    price: float | None = None
    change_pct: float | None = None
    turnover_pct: float | None = None
    total_market_cap_yi: float | None = None


@dataclasses.dataclass(slots=True)
class Signal:
    group: str
    code: str
    name: str
    date: str
    close: float
    score: float
    setup_date: str
    setup_low: float
    gain_from_setup_low: float
    pct_5d: float
    pct_20d: float
    volume_ratio_5_20: float
    turnover_pct: float | None
    total_market_cap_yi: float | None
    reasons: list[str]


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str | None) -> dict[str, Any]:
    if not path:
        return DEFAULT_CONFIG
    with open(path, "r", encoding="utf-8") as f:
        user_config = json.load(f)
    return deep_merge(DEFAULT_CONFIG, user_config)


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    if "date" not in df.columns and "日期" not in df.columns:
        df = df.reset_index()
    mapping = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "振幅": "amplitude",
        "涨跌幅": "pct_chg",
        "涨跌额": "change",
        "换手率": "turnover",
    }
    df = df.rename(columns={k: v for k, v in mapping.items() if k in df.columns}).copy()
    required = ["date", "open", "close", "high", "low", "volume"]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"missing columns: {missing}")
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "close", "high", "low", "volume", "amount", "turnover"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=required).sort_values("date").reset_index(drop=True)


def coalesce_duplicate_columns(df: pd.DataFrame, column: str) -> pd.DataFrame:
    matches = df.loc[:, df.columns == column]
    if matches.shape[1] <= 1:
        return df
    merged = matches.bfill(axis=1).iloc[:, 0]
    out = df.loc[:, df.columns != column].copy()
    out[column] = merged
    return out


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = out["close"]
    volume = out["volume"]
    for window in [5, 10, 13, 20, 35, 55, 60, 120]:
        out[f"ma{window}"] = close.rolling(window).mean()
    out["vol_ma5"] = volume.rolling(5).mean()
    out["vol_ma10"] = volume.rolling(10).mean()
    out["vol_ma20"] = volume.rolling(20).mean()

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    out["diff"] = ema12 - ema26
    out["dea"] = out["diff"].ewm(span=9, adjust=False).mean()
    out["macd"] = (out["diff"] - out["dea"]) * 2

    low_min = out["low"].rolling(9).min()
    high_max = out["high"].rolling(9).max()
    rsv = (close - low_min) / (high_max - low_min).replace(0, np.nan) * 100
    out["k"] = rsv.ewm(com=2, adjust=False).mean()
    out["d"] = out["k"].ewm(com=2, adjust=False).mean()
    out["j"] = 3 * out["k"] - 2 * out["d"]

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(6).mean()
    avg_loss = loss.rolling(6).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out["rsi6"] = 100 - 100 / (1 + rs)
    out.loc[(avg_loss == 0) & (avg_gain > 0), "rsi6"] = 100
    out.loc[(avg_loss == 0) & (avg_gain == 0), "rsi6"] = 50
    return out


def pct_change_from(df: pd.DataFrame, days: int) -> float:
    if len(df) <= days:
        return 0.0
    base = float(df["close"].iloc[-days - 1])
    if base <= 0:
        return 0.0
    return float(df["close"].iloc[-1] / base - 1)


def star_setup_mask(df: pd.DataFrame, config: dict[str, Any]) -> pd.Series:
    sv = config["star_volume"]
    lookback = int(sv["lookback_days"])
    shrink_ratio = float(sv["volume_shrink_ratio"])
    tight_range = float(sv["tight_price_range"])
    near_low_ratio = float(sv["near_low_ratio"])
    max_gain_20d = float(sv["max_recent_gain_20d"])

    recent_high = df["high"].rolling(lookback).max()
    recent_low = df["low"].rolling(lookback).min()
    range_ratio = (recent_high - recent_low) / recent_low.replace(0, np.nan)
    volume_shrink = df["vol_ma5"] <= df["vol_ma20"] * shrink_ratio

    low_120 = df["low"].rolling(120, min_periods=60).min()
    high_120 = df["high"].rolling(120, min_periods=60).max()
    position = (df["close"] - low_120) / (high_120 - low_120).replace(0, np.nan)

    gain_20 = df["close"].pct_change(20)
    trend_repair = (
        (df["close"] >= df["ma13"] * 0.96)
        & (df["macd"] >= df["macd"].shift(1))
        & (df["rsi6"] >= df["rsi6"].shift(1))
    )

    return (
        volume_shrink
        & (range_ratio <= tight_range)
        & (position <= near_low_ratio)
        & (gain_20 <= max_gain_20d)
        & trend_repair
    ).fillna(False)


def score_star_candidate(df: pd.DataFrame, idx: int, config: dict[str, Any]) -> tuple[float, list[str]]:
    row = df.iloc[idx]
    reasons: list[str] = []
    score = 0.0

    vol_ratio = safe_ratio(row.get("vol_ma5"), row.get("vol_ma20"))
    if vol_ratio <= 0.35:
        score += 24
        reasons.append("5日均量低于20日均量35%，缩量很明显")
    elif vol_ratio <= 0.55:
        score += 18
        reasons.append("成交量连续收缩，接近图片里的星星量")
    else:
        score += 8

    recent = df.iloc[max(0, idx - 11) : idx + 1]
    range_ratio = safe_ratio(recent["high"].max() - recent["low"].min(), recent["low"].min())
    if range_ratio <= 0.10:
        score += 18
        reasons.append("近12日价格收得很窄")
    elif range_ratio <= 0.16:
        score += 12
        reasons.append("近12日波动收窄")

    low_120 = df["low"].iloc[max(0, idx - 119) : idx + 1].min()
    high_120 = df["high"].iloc[max(0, idx - 119) : idx + 1].max()
    position = safe_ratio(row["close"] - low_120, high_120 - low_120)
    if position <= 0.18:
        score += 18
        reasons.append("股价靠近120日阶段低位")
    elif position <= 0.28:
        score += 12
        reasons.append("仍在阶段低位区")

    if idx >= 2 and df["macd"].iloc[idx] > df["macd"].iloc[idx - 1] > df["macd"].iloc[idx - 2]:
        score += 12
        reasons.append("MACD柱连续改善")
    if idx >= 1 and row["close"] >= row["ma13"] and df["close"].iloc[idx - 1] < df["ma13"].iloc[idx - 1]:
        score += 10
        reasons.append("收盘重新站上13日线")
    elif row["close"] >= row["ma13"] * 0.98:
        score += 6
        reasons.append("价格贴近13日线修复")
    if idx >= 1 and row["rsi6"] > df["rsi6"].iloc[idx - 1]:
        score += 6
        reasons.append("RSI短线回升")

    return round(score, 1), reasons


def safe_ratio(numerator: Any, denominator: Any) -> float:
    try:
        numerator_f = float(numerator)
        denominator_f = float(denominator)
    except (TypeError, ValueError):
        return 0.0
    if denominator_f == 0 or np.isnan(denominator_f):
        return 0.0
    return numerator_f / denominator_f


def latest_signal(meta: StockMeta, raw_df: pd.DataFrame, config: dict[str, Any]) -> Signal | None:
    config = deep_merge(DEFAULT_CONFIG, config)
    if len(raw_df) < 130:
        return None
    df = add_indicators(normalize_columns(raw_df))
    if len(df) < 130:
        return None

    mask = star_setup_mask(df, config)
    min_setup_days = int(config["star_volume"]["min_setup_days"])
    recent_setup_days = int(mask.tail(20).sum())
    latest_idx = len(df) - 1
    pct_5d = pct_change_from(df, 5)
    pct_20d = pct_change_from(df, 20)
    latest = df.iloc[-1]

    breakout_cfg = config["breakout"]
    confirm_window = int(breakout_cfg["confirm_window_days"])
    recent_start = max(0, latest_idx - confirm_window + 1)
    setup_indices = np.flatnonzero(mask.iloc[recent_start : latest_idx + 1].to_numpy()) + recent_start
    if len(setup_indices) == 0:
        return None

    setup_idx = int(setup_indices[0])
    setup_window = df.iloc[max(0, setup_idx - 5) : setup_idx + 1]
    setup_low = float(setup_window["low"].min())
    gain_from_setup_low = safe_ratio(float(latest["close"]) - setup_low, setup_low)
    volume_burst = safe_ratio(latest["vol_ma5"], latest["vol_ma20"])

    setup_score, reasons = score_star_candidate(df, setup_idx, config)
    is_breakout = (
        gain_from_setup_low >= float(breakout_cfg["min_gain_from_setup_low"])
        and (pct_5d >= float(breakout_cfg["min_recent_gain_5d"]) or volume_burst >= float(breakout_cfg["volume_burst_ratio"]))
        and latest["close"] >= min(latest["ma35"], latest["ma55"])
    )

    if is_breakout:
        score = setup_score + min(gain_from_setup_low * 60, 30) + min(volume_burst * 8, 18)
        group = "已暴涨验证组"
        reasons = reasons + [
            f"星星量后从低点上涨{gain_from_setup_low:.1%}",
            f"5日/20日均量比{volume_burst:.2f}",
        ]
    else:
        currently_star = bool(recent_setup_days >= min_setup_days or mask.iloc[-1])
        not_overheated = pct_20d <= float(config["star_volume"]["max_recent_gain_20d"])
        turning_up = (
            latest["macd"] >= df["macd"].iloc[-2]
            and latest["close"] >= latest["ma13"] * 0.96
            and latest["rsi6"] >= df["rsi6"].iloc[-2]
        )
        if not (currently_star and not_overheated and turning_up):
            return None
        score = setup_score + recent_setup_days * 2 + max(0.0, latest["macd"] - df["macd"].iloc[-2]) * 3
        group = "有暴涨趋势组"
        reasons = reasons + [f"近20日出现{recent_setup_days}天星星量特征"]

    return Signal(
        group=group,
        code=meta.code,
        name=meta.name,
        date=latest["date"].strftime("%Y-%m-%d"),
        close=round(float(latest["close"]), 2),
        score=round(float(score), 1),
        setup_date=df.iloc[setup_idx]["date"].strftime("%Y-%m-%d"),
        setup_low=round(setup_low, 2),
        gain_from_setup_low=round(gain_from_setup_low, 4),
        pct_5d=round(pct_5d, 4),
        pct_20d=round(pct_20d, 4),
        volume_ratio_5_20=round(volume_burst, 2),
        turnover_pct=meta.turnover_pct,
        total_market_cap_yi=meta.total_market_cap_yi,
        reasons=reasons[:4],
    )


def fetch_universe(config: dict[str, Any]) -> list[StockMeta]:
    import akshare as ak

    try:
        spot = ak.stock_zh_a_spot_em()
    except Exception as exc:  # noqa: BLE001
        print(f"东方财富行情列表获取失败，改用备用股票列表：{exc}", file=sys.stderr)
        fallback_frames: list[pd.DataFrame] = []
        for getter, kwargs in [
            (ak.stock_info_sh_name_code, {"symbol": "主板A股"}),
            (ak.stock_info_sh_name_code, {"symbol": "科创板"}),
            (ak.stock_info_sz_name_code, {"symbol": "A股列表"}),
        ]:
            try:
                fallback_frames.append(getter(**kwargs))
            except Exception as fallback_exc:  # noqa: BLE001
                print(f"备用股票列表部分失败，已跳过：{fallback_exc}", file=sys.stderr)
        if not fallback_frames:
            raise
        spot = pd.concat(fallback_frames, ignore_index=True)

    cols = {
        "代码": "code",
        "名称": "name",
        "证券代码": "code",
        "证券简称": "name",
        "A股代码": "code",
        "A股简称": "name",
        "最新价": "price",
        "涨跌幅": "change_pct",
        "换手率": "turnover_pct",
        "总市值": "total_market_cap",
    }
    spot = spot.rename(columns={k: v for k, v in cols.items() if k in spot.columns})
    spot = coalesce_duplicate_columns(spot, "code")
    spot = coalesce_duplicate_columns(spot, "name")
    required = {"code", "name"}
    if not required.issubset(spot.columns):
        raise RuntimeError(f"行情列表字段异常，当前字段：{list(spot.columns)}")

    filters = config["filters"]
    if "price" in spot.columns:
        spot["price"] = pd.to_numeric(spot["price"], errors="coerce")
        spot = spot[(spot["price"] >= filters["min_price"]) & (spot["price"] <= filters["max_price"])]
    if "total_market_cap" in spot.columns:
        spot["total_market_cap_yi"] = pd.to_numeric(spot["total_market_cap"], errors="coerce") / 100_000_000
        spot = spot[
            (spot["total_market_cap_yi"] >= filters["min_total_market_cap_yi"])
            & (spot["total_market_cap_yi"] <= filters["max_total_market_cap_yi"])
        ]
    else:
        spot["total_market_cap_yi"] = np.nan

    if config["market"].get("exclude_st", True):
        spot = spot[~spot["name"].astype(str).str.contains("ST", case=False, na=False)]
    if config["market"].get("exclude_beijing", True):
        spot = spot[~spot["code"].astype(str).str.startswith(("8", "4", "9"))]

    metas: list[StockMeta] = []
    for _, row in spot.iterrows():
        metas.append(
            StockMeta(
                code=str(row["code"]).zfill(6),
                name=str(row["name"]),
                price=to_float(row.get("price")),
                change_pct=to_float(row.get("change_pct")),
                turnover_pct=to_float(row.get("turnover_pct")),
                total_market_cap_yi=to_float(row.get("total_market_cap_yi")),
            )
        )
    return metas


def to_float(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_history(meta: StockMeta, config: dict[str, Any]) -> pd.DataFrame:
    import akshare as ak

    days = int(config["market"]["history_days"])
    start = (dt.date.today() - dt.timedelta(days=int(days * 1.8))).strftime("%Y%m%d")
    end = dt.date.today().strftime("%Y%m%d")
    adjust = str(config["market"]["adjust"])
    try:
        return ak.stock_zh_a_hist(
            symbol=meta.code,
            period="daily",
            start_date=start,
            end_date=end,
            adjust=adjust,
        )
    except Exception:
        prefix = "sh" if meta.code.startswith("6") else "sz"
        return ak.stock_zh_a_daily(
            symbol=f"{prefix}{meta.code}",
            start_date=start,
            end_date=end,
            adjust=adjust,
        )


def scan_one(meta: StockMeta, config: dict[str, Any]) -> tuple[Signal | None, str | None]:
    try:
        delay = float(config["market"].get("request_delay_seconds", 0))
        if delay > 0:
            time.sleep(delay)
        history = fetch_history(meta, config)
        return latest_signal(meta, history, config), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{meta.code} {meta.name}: {exc}"


def scan_market(config: dict[str, Any], limit: int | None = None) -> tuple[list[Signal], list[str]]:
    metas = fetch_universe(config)
    if limit:
        metas = metas[:limit]

    signals: list[Signal] = []
    errors: list[str] = []
    max_workers = int(config["market"]["max_workers"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(scan_one, meta, config): meta for meta in metas}
        for i, future in enumerate(concurrent.futures.as_completed(future_map), start=1):
            signal, error = future.result()
            if signal:
                signals.append(signal)
            if error:
                errors.append(error)
            if i % 200 == 0:
                print(f"已扫描 {i}/{len(metas)}，命中 {len(signals)}", flush=True)

    return signals, errors


def group_signals(signals: list[Signal], top_n: int) -> dict[str, list[Signal]]:
    groups = {
        "已暴涨验证组": [],
        "有暴涨趋势组": [],
    }
    for signal in signals:
        groups.setdefault(signal.group, []).append(signal)
    for key in groups:
        groups[key] = sorted(groups[key], key=lambda item: item.score, reverse=True)[:top_n]
    return groups


def format_signal(signal: Signal, index: int) -> str:
    cap = "" if signal.total_market_cap_yi is None else f"，市值{signal.total_market_cap_yi:.0f}亿"
    turnover = "" if signal.turnover_pct is None else f"，换手{signal.turnover_pct:.2f}%"
    reasons = "；".join(signal.reasons)
    return (
        f"{index}. {signal.name}({signal.code}) 收{signal.close}，评分{signal.score}"
        f"，星星量日{signal.setup_date}，低点{signal.setup_low}"
        f"，低点以来{signal.gain_from_setup_low:.1%}"
        f"，5日{signal.pct_5d:.1%}，20日{signal.pct_20d:.1%}"
        f"，量比{signal.volume_ratio_5_20:.2f}{turnover}{cap}\n"
        f"   理由：{reasons}"
    )


def build_report(signals: list[Signal], errors: list[str], config: dict[str, Any]) -> tuple[str, str]:
    top_n = int(config["filters"]["top_n"])
    groups = group_signals(signals, top_n)
    today = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    title = f"AKI星星量股票筛选 {today}"
    parts = [
        f"# {title}",
        "",
        "说明：这是按“低位连续缩量、价格收窄、指标修复、放量突破”编程筛出来的观察名单，不是买卖建议。",
        "",
    ]
    for group_name in ["已暴涨验证组", "有暴涨趋势组"]:
        items = groups[group_name]
        parts.append(f"## {group_name}（{len(items)}只）")
        if not items:
            parts.append("今天没有命中。")
        else:
            parts.extend(format_signal(item, i) for i, item in enumerate(items, start=1))
        parts.append("")

    if errors:
        parts.append(f"## 数据提醒")
        parts.append(f"有 {len(errors)} 只股票拉取失败，已跳过；通常是行情源临时波动。")
        parts.append("")
    return title, "\n".join(parts).strip()


def push_server_chan(title: str, desp: str) -> None:
    sendkey = os.getenv("AKI_SERVER_CHAN_SENDKEY")
    if not sendkey:
        raise RuntimeError("未配置 AKI_SERVER_CHAN_SENDKEY")
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    response = requests.post(url, data={"title": title, "desp": desp}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    code = payload.get("code")
    if code not in (0, "0"):
        raise RuntimeError(f"Server酱推送失败：{payload}")


def save_report(report: str, output_dir: str = "AKI_reports") -> Path:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"star_volume_{dt.date.today().isoformat()}.md"
    path.write_text(report, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="筛选A股星星量股票并用Server酱推送")
    parser.add_argument("--config", help="配置文件路径，默认使用内置配置")
    parser.add_argument("--no-push", action="store_true", help="只生成报告，不推送")
    parser.add_argument("--limit", type=int, help="只扫描前N只股票，方便调试")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    signals, errors = scan_market(config, limit=args.limit)
    title, report = build_report(signals, errors, config)
    path = save_report(report)
    print(report)
    print(f"\n报告已保存：{path}")

    push_enabled = bool(config.get("push", {}).get("enabled", True))
    if push_enabled and not args.no_push:
        push_server_chan(title, report)
        print("Server酱推送完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
