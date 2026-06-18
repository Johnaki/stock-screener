import concurrent.futures
import datetime as dt
import math
import os
import time

import akshare as ak
import pandas as pd
import requests


# 正样本只用来校准模型。正式筛选结果会排除这些股票。
# zone 是你确认或截图里能明显看到的“长时间低成交额平躺区”。
SAMPLES = [
    {"code": "001696", "name": "宗申动力", "zone": ("2026-03-31", "2026-06-03"), "note": "你已确认"},
    {"code": "002141", "name": "贤丰控股", "zone": None, "note": "参考样本"},
    {"code": "301303", "name": "真兰仪表", "zone": None, "note": "参考样本"},
    {"code": "000032", "name": "深桑达A", "zone": None, "note": "参考样本"},
    {"code": "603011", "name": "合锻智能", "zone": None, "note": "参考样本"},
    {"code": "300481", "name": "濮阳惠成", "zone": None, "note": "参考样本"},
    {"code": "300401", "name": "花园生物", "zone": None, "note": "参考样本"},
]
SAMPLE_CODES = {x["code"] for x in SAMPLES}

MAX_STOCKS = int(os.getenv("AKI_MAX_STOCKS", "900"))
MAX_WORKERS = int(os.getenv("AKI_MAX_WORKERS", "5"))

# 星星量不是几天的小缩量，而是一段较长的“成交额贴地、均量线平躺”。
MIN_STAR_DAYS = 28
MAX_STAR_DAYS = 78
SCAN_LOOKBACK_DAYS = 170


def push_server_chan(title, text):
    key = os.getenv("AKI_SERVER_CHAN_SENDKEY")
    if not key:
        raise RuntimeError("没有设置 AKI_SERVER_CHAN_SENDKEY")
    url = f"https://sctapi.ftqq.com/{key}.send"
    response = requests.post(url, data={"title": title, "desp": text}, timeout=30)
    print(response.text)
    response.raise_for_status()


def fetch_history(code):
    end = dt.date.today().strftime("%Y%m%d")
    start = (dt.date.today() - dt.timedelta(days=620)).strftime("%Y%m%d")
    prefix = "sh" if code.startswith("6") else "sz"
    symbol = f"{prefix}{code}"

    errors = []
    try:
        return ak.stock_zh_a_daily(symbol=symbol, start_date=start, end_date=end, adjust="qfq")
    except Exception as exc:
        errors.append(f"新浪:{exc}")
    try:
        return ak.stock_zh_a_hist_tx(symbol=symbol, start_date=start, end_date=end, adjust="qfq", timeout=20)
    except Exception as exc:
        errors.append(f"腾讯:{exc}")
    try:
        return ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq", timeout=20)
    except Exception as exc:
        errors.append(f"东财:{exc}")
    raise RuntimeError(" / ".join(errors))


def prepare(df):
    rename_map = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "date": "date",
        "open": "open",
        "close": "close",
        "high": "high",
        "low": "low",
        "volume": "volume",
        "amount": "amount",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    if "date" not in df.columns:
        df = df.reset_index().rename(columns={"index": "date"})

    for col in ["open", "close", "high", "low", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    else:
        df["amount"] = df["volume"] * df["close"]

    # 有的接口成交额单位不同。这里不做绝对金额判断，只做同一只股票内部比例。
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "open", "close", "high", "low", "volume", "amount"])
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    df = df[df["amount"] > 0].reset_index(drop=True)

    df["a3"] = df["amount"].rolling(3).mean()
    df["a5"] = df["amount"].rolling(5).mean()
    df["a10"] = df["amount"].rolling(10).mean()
    df["a20"] = df["amount"].rolling(20).mean()
    df["a60"] = df["amount"].rolling(60).mean()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    return df


def safe_ratio(a, b):
    if b is None or pd.isna(b) or abs(float(b)) < 1e-12:
        return 0.0
    return float(a) / float(b)


def linear_slope_ratio(values):
    values = pd.Series(values).dropna()
    if len(values) < 8:
        return 0.0
    y = values.astype(float).to_list()
    n = len(y)
    x_mean = (n - 1) / 2
    y_mean = sum(y) / n
    denom = sum((i - x_mean) ** 2 for i in range(n))
    if denom == 0 or abs(y_mean) < 1e-12:
        return 0.0
    slope = sum((i - x_mean) * (y[i] - y_mean) for i in range(n)) / denom
    return slope * n / y_mean


def date_index(df, day):
    day = pd.to_datetime(day)
    hits = df.index[df["date"] >= day].tolist()
    return hits[0] if hits else None


def feature_from_window(df, start, length):
    end = start + length - 1
    if start < 80 or end >= len(df):
        return None

    win = df.iloc[start : end + 1]
    before = df.iloc[max(0, start - 90) : start]
    context = df.iloc[max(0, start - 90) : end + 1]
    after = df.iloc[end + 1 :]
    if len(win) < MIN_STAR_DAYS or len(context) < 90:
        return None

    star_low = float(win["low"].min())
    star_high = float(win["high"].max())
    context_low = float(context["low"].min())
    context_high = float(context["high"].max())
    win_amount_mid = float(win["amount"].median())
    context_amount_mid = float(context["amount"].median())
    before_amount_mid = float(before["amount"].median()) if len(before) >= 20 else context_amount_mid

    a5 = win["a5"].dropna()
    a10 = win["a10"].dropna()
    amount_line = a5 if len(a5) >= 12 else win["amount"]
    amount_slope = linear_slope_ratio(amount_line)
    amount_flatness = safe_ratio(amount_line.quantile(0.90), amount_line.quantile(0.25))
    price_slope = linear_slope_ratio(win["close"])

    low_line_days = float((win["amount"] <= context["amount"].quantile(0.45)).mean())
    calm_days = float((win["amount"] <= context["amount"].quantile(0.60)).mean())
    red_spike_days = float((win["amount"] >= context["amount"].quantile(0.82)).mean())

    pos = safe_ratio(win["close"].iloc[-1] - context_low, context_high - context_low)
    tight = safe_ratio(star_high - star_low, star_low)
    amount_ratio = safe_ratio(win_amount_mid, context_amount_mid)
    before_ratio = safe_ratio(win_amount_mid, before_amount_mid)
    gain_to_now = safe_ratio(df["close"].iloc[-1], star_low) - 1
    days_after = len(df) - 1 - end

    if len(after) > 0:
        after10 = after.head(10)
        after25 = after.head(25)
        launch_amount = safe_ratio(after10["amount"].max(), win_amount_mid)
        gain_after_25 = safe_ratio(after25["close"].max(), star_low) - 1
        launch_days = int((after10["amount"] >= win_amount_mid * 1.8).sum())
    else:
        launch_amount = 0.0
        gain_after_25 = 0.0
        launch_days = 0

    pct5 = safe_ratio(df["close"].iloc[-1], df["close"].iloc[-6]) - 1 if len(df) >= 6 else 0.0
    pct20 = safe_ratio(df["close"].iloc[-1], df["close"].iloc[-21]) - 1 if len(df) >= 21 else 0.0
    current_amount_ratio = safe_ratio(df["a5"].iloc[-1], win_amount_mid)
    current_price_pos = safe_ratio(df["close"].iloc[-1] - context_low, context_high - context_low)

    return {
        "start_idx": start,
        "end_idx": end,
        "start": win["date"].iloc[0].strftime("%Y-%m-%d"),
        "end": win["date"].iloc[-1].strftime("%Y-%m-%d"),
        "length": length,
        "pos": pos,
        "current_price_pos": current_price_pos,
        "tight": tight,
        "amount_ratio": amount_ratio,
        "before_ratio": before_ratio,
        "low_line_days": low_line_days,
        "calm_days": calm_days,
        "red_spike_days": red_spike_days,
        "amount_slope": amount_slope,
        "amount_flatness": amount_flatness,
        "price_slope": price_slope,
        "gain_to_now": gain_to_now,
        "gain_after_25": gain_after_25,
        "launch_amount": launch_amount,
        "launch_days": launch_days,
        "current_amount_ratio": current_amount_ratio,
        "pct5": pct5,
        "pct20": pct20,
        "days_after": days_after,
    }


def base_star_filter(f):
    # 长时间低成交额、均量线贴地，价格不能已经大幅脱离低位。
    return (
        MIN_STAR_DAYS <= f["length"] <= MAX_STAR_DAYS
        and f["amount_ratio"] <= 0.62
        and f["before_ratio"] <= 0.72
        and f["low_line_days"] >= 0.56
        and f["calm_days"] >= 0.72
        and f["red_spike_days"] <= 0.18
        and f["amount_flatness"] <= 2.80
        and f["amount_slope"] <= 0.38
        and f["tight"] <= 0.50
        and f["pos"] <= 0.72
    )


def score_long_star(f):
    score = 100.0
    score += min(f["length"], 70) * 0.45
    score += max(0, 0.62 - f["amount_ratio"]) * 55
    score += max(0, 0.72 - f["before_ratio"]) * 35
    score += f["low_line_days"] * 35
    score += f["calm_days"] * 25
    score += max(0, 2.8 - f["amount_flatness"]) * 12
    score -= max(0, f["amount_slope"]) * 25
    score -= max(0, f["tight"] - 0.34) * 60
    score -= f["red_spike_days"] * 55
    return score


def sample_like_score(f, template):
    keys = {
        "length": 0.018,
        "amount_ratio": 1.5,
        "before_ratio": 1.1,
        "low_line_days": 1.0,
        "amount_flatness": 0.34,
        "amount_slope": 0.85,
        "tight": 0.95,
        "pos": 0.55,
        "price_slope": 0.45,
    }
    dist = 0.0
    for key, weight in keys.items():
        dist += weight * abs(float(f[key]) - float(template[key]))
    return max(0.0, 100.0 - dist * 42.0)


def classify_window(f):
    # 已经开始涨：必须从长低量区之后，出现放量和价格脱离。
    launched = (
        1 <= f["days_after"] <= 70
        and f["gain_to_now"] >= 0.22
        and f["gain_after_25"] >= 0.18
        and f["launch_amount"] >= 1.80
        and f["launch_days"] >= 1
    )
    # 还趴着：窗口要接近现在，成交额仍低，价格还没明显启动。
    ready = (
        f["days_after"] <= 4
        and f["gain_to_now"] <= 0.16
        and f["pct20"] <= 0.16
        and f["current_amount_ratio"] <= 1.45
        and f["current_price_pos"] <= 0.74
    )
    if launched:
        return "已开始涨验证组"
    if ready:
        return "仍在星星量低位组"
    return None


def find_best_long_window(df, templates=None, fixed_zone=None, sample_mode=False):
    if fixed_zone:
        start = date_index(df, fixed_zone[0])
        end = date_index(df, fixed_zone[1])
        if start is None or end is None or end <= start:
            return None
        f = feature_from_window(df, start, end - start + 1)
        if f:
            f["fixed_zone"] = True
        return f

    best = None
    min_start = max(80, len(df) - SCAN_LOOKBACK_DAYS)
    max_start = max(min_start, len(df) - MIN_STAR_DAYS)
    for length in range(MIN_STAR_DAYS, MAX_STAR_DAYS + 1):
        for start in range(min_start, max_start + 1):
            if start + length > len(df):
                continue
            f = feature_from_window(df, start, length)
            if not f or not base_star_filter(f):
                continue
            group = classify_window(f)
            if not sample_mode and group is None:
                continue

            if templates:
                scores = [(sample_like_score(f, t), t["sample_name"]) for t in templates]
                similarity, sample_name = max(scores, key=lambda x: x[0])
                if similarity < 70:
                    continue
            else:
                similarity, sample_name = 100.0, "样本"

            score = score_long_star(f) + similarity * 0.70
            if group == "已开始涨验证组":
                score += min(f["gain_to_now"], 1.2) * 25 + min(f["launch_amount"], 6) * 3
            elif group == "仍在星星量低位组":
                score += max(0, 1.45 - f["current_amount_ratio"]) * 18

            f = dict(f)
            f["group"] = group or "样本模板"
            f["similarity"] = similarity
            f["sample_name"] = sample_name
            f["score"] = score
            f["fixed_zone"] = False
            if best is None or f["score"] > best["score"]:
                best = f
    return best


def build_templates():
    templates = []
    lines = []
    for sample in SAMPLES:
        code = sample["code"]
        name = sample["name"]
        try:
            df = prepare(fetch_history(code))
            f = find_best_long_window(df, fixed_zone=sample["zone"], sample_mode=True)
            if f is None:
                f = find_best_long_window(df, sample_mode=True)
            if f:
                f = dict(f)
                f["sample_code"] = code
                f["sample_name"] = name
                templates.append(f)
                tag = "人工校准" if sample["zone"] else "程序参考"
                lines.append(
                    f"- {name}({code})：{tag}，星星量长区 {f['start']} 至 {f['end']}，"
                    f"{f['length']}个交易日，低量天数{f['low_line_days']:.0%}，成交额比{f['amount_ratio']:.2f}，后涨幅{f['gain_to_now']:.1%}"
                )
            else:
                lines.append(f"- {name}({code})：没有建立模板")
        except Exception as exc:
            lines.append(f"- {name}({code})：数据失败 {exc}")
        time.sleep(0.08)
    return templates, lines


def get_stock_list():
    errors = []
    try:
        d = ak.stock_zh_a_spot_em()
        d = d.rename(columns={"代码": "code", "名称": "name", "最新价": "price", "总市值": "cap"})
        d["code"] = d["code"].astype(str).str.zfill(6)
        d["name"] = d["name"].astype(str)
        d["price"] = pd.to_numeric(d["price"], errors="coerce")
        d["cap_yi"] = pd.to_numeric(d["cap"], errors="coerce") / 100000000
        d = d[~d["name"].str.contains("ST|退", case=False, na=False)]
        d = d[~d["code"].str.startswith(("8", "4", "9"))]
        d = d[(d["price"] >= 2.5) & (d["price"] <= 180)]
        d = d[(d["cap_yi"].isna()) | ((d["cap_yi"] >= 8) & (d["cap_yi"] <= 3000))]
        return d[["code", "name"]].drop_duplicates("code").head(MAX_STOCKS).to_dict("records")
    except Exception as exc:
        errors.append(f"东方财富实时列表失败：{exc}")

    try:
        d = ak.stock_info_a_code_name()
        d = d.rename(columns={"code": "code", "name": "name", "代码": "code", "名称": "name"})
        d["code"] = d["code"].astype(str).str.zfill(6)
        d["name"] = d["name"].astype(str)
        d = d[~d["name"].str.contains("ST|退", case=False, na=False)]
        d = d[~d["code"].str.startswith(("8", "4", "9"))]
        return d[["code", "name"]].drop_duplicates("code").head(MAX_STOCKS).to_dict("records")
    except Exception as exc:
        errors.append(f"A股代码名称列表失败：{exc}")

    # 不再用 000001、000002 这种硬凑代码段。那会扫到大量不存在的股票，跑两小时也没意义。
    raise RuntimeError("无法获取真实股票列表，已停止扫描；原因：" + "；".join(errors))


def scan_item(item, templates):
    code = item["code"]
    name = item["name"]
    try:
        df = prepare(fetch_history(code))
        if len(df) < 140:
            return None
        f = find_best_long_window(df, templates=templates)
        if not f:
            return None
        f = dict(f)
        f["code"] = code
        f["name"] = name
        f["close"] = float(df["close"].iloc[-1])
        return f
    except Exception as exc:
        print(code, name, "失败", exc)
        return None


def scan_market(templates):
    items = [x for x in get_stock_list() if x["code"] not in SAMPLE_CODES]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {executor.submit(scan_item, item, templates): item for item in items}
        for i, future in enumerate(concurrent.futures.as_completed(future_map), 1):
            result = future.result()
            if result:
                results.append(result)
            if i % 200 == 0:
                print(f"已扫描 {i}/{len(items)}，命中 {len(results)}")
    return items, results


def format_item(x, idx):
    return (
        f"{idx}. {x['name']}({x['code']}) 收{x['close']:.2f}，评分{x['score']:.1f}，相似{x['sample_name']} {x['similarity']:.0f}%\n"
        f"   星星量长区：{x['start']} 至 {x['end']}，{x['length']}个交易日，之后{int(x['days_after'])}天\n"
        f"   形态：成交额比{x['amount_ratio']:.2f}，低量天数{x['low_line_days']:.0%}，成交额平稳{x['amount_flatness']:.2f}，振幅{x['tight']:.1%}\n"
        f"   状态：后涨幅{x['gain_to_now']:.1%}，5日{x['pct5']:.1%}，20日{x['pct20']:.1%}，启动放量{x['launch_amount']:.1f}倍"
    )


def main():
    templates, sample_lines = build_templates()
    if not templates:
        raise RuntimeError("样本模板没有建立成功，停止扫描")

    items, results = scan_market(templates)
    launched = sorted([x for x in results if x["group"] == "已开始涨验证组"], key=lambda x: x["score"], reverse=True)[:25]
    ready = sorted([x for x in results if x["group"] == "仍在星星量低位组"], key=lambda x: x["score"], reverse=True)[:25]

    title = "AKI星星量长区筛选 " + dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# {title}",
        "",
        "说明：这版按“长时间低成交额平躺区”筛，不再把几天的小缩量当星星量。样本只建模板，正式结果已排除样本股；不是买卖建议。",
        "",
        "## 样本模板验证",
    ]
    lines.extend(sample_lines)
    lines.append("")

    for group, rows in [("正式筛选：已开始涨验证组", launched), ("正式筛选：仍在星星量低位组", ready)]:
        lines.append(f"## {group}（{len(rows)}只）")
        lines.extend(["今天没有命中。"] if not rows else [format_item(x, i) for i, x in enumerate(rows, 1)])
        lines.append("")

    lines.append(f"正式扫描 {len(items)} 只，命中 {len(results)} 只。")
    text = "\n".join(lines)
    print(text)
    push_server_chan(title, text)


if __name__ == "__main__":
    main()
