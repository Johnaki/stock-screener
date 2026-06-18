import concurrent.futures
import datetime as dt
import math
import os
import time

import akshare as ak
import pandas as pd
import requests


SAMPLES = [
    ("001696", "宗申动力"),
    ("002141", "贤丰控股"),
    ("301303", "真兰仪表"),
    ("000032", "深桑达A"),
    ("603011", "合锻智能"),
    ("300481", "濮阳惠成"),
    ("300401", "花园生物"),
]
SAMPLE_CODES = {code for code, _ in SAMPLES}

MAX_STOCKS = int(os.getenv("AKI_MAX_STOCKS", "2200"))
MAX_WORKERS = int(os.getenv("AKI_MAX_WORKERS", "6"))


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
    start = (dt.date.today() - dt.timedelta(days=560)).strftime("%Y%m%d")
    prefix = "sh" if code.startswith("6") else "sz"
    symbol = f"{prefix}{code}"
    try:
        return ak.stock_zh_a_daily(
            symbol=symbol,
            start_date=start,
            end_date=end,
            adjust="qfq",
        )
    except Exception as exc:
        print(code, "新浪历史行情失败，改用腾讯：", exc)
    try:
        return ak.stock_zh_a_hist_tx(
            symbol=symbol,
            start_date=start,
            end_date=end,
            adjust="qfq",
            timeout=15,
        )
    except Exception as exc:
        print(code, "腾讯历史行情失败，改用东方财富：", exc)
    return ak.stock_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=start,
        end_date=end,
        adjust="qfq",
        timeout=15,
    )


def prepare(df):
    if "日期" in df.columns:
        df = df.rename(
            columns={
                "日期": "date",
                "开盘": "open",
                "收盘": "close",
                "最高": "high",
                "最低": "low",
                "成交量": "volume",
                "成交额": "amount",
            }
        )
    if "date" not in df.columns:
        df = df.reset_index().rename(columns={"index": "date"})
    for col in ["open", "close", "high", "low", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    else:
        df["amount"] = df["volume"] * df["close"]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "open", "close", "high", "low", "volume", "amount"])
    df = df.sort_values("date").reset_index(drop=True)
    df["a5"] = df["amount"].rolling(5).mean()
    df["a20"] = df["amount"].rolling(20).mean()
    df["a60"] = df["amount"].rolling(60).mean()
    df["ma13"] = df["close"].rolling(13).mean()
    df["ma35"] = df["close"].rolling(35).mean()
    e12 = df["close"].ewm(span=12, adjust=False).mean()
    e26 = df["close"].ewm(span=26, adjust=False).mean()
    diff = e12 - e26
    df["macd"] = (diff - diff.ewm(span=9, adjust=False).mean()) * 2
    return df


def safe_ratio(a, b):
    if b is None or pd.isna(b) or abs(float(b)) < 1e-9:
        return 0.0
    return float(a) / float(b)


def feature_from_window(df, start, length):
    end = start + length - 1
    if start < 0 or end + 3 >= len(df):
        return None
    win = df.iloc[start : end + 1]
    base = df.iloc[max(0, end - 119) : end + 1]
    after = df.iloc[end + 1 :]
    if len(win) < 8 or len(base) < 70 or len(after) < 3:
        return None

    pos = safe_ratio(win["close"].iloc[-1] - base["low"].min(), base["high"].max() - base["low"].min())
    tight = safe_ratio(win["high"].max() - win["low"].min(), win["low"].min())
    amount_ratio = safe_ratio(win["amount"].median(), base["amount"].median())
    quiet_days = float((win["amount"] <= base["amount"].quantile(0.55)).mean())
    pre_gain = safe_ratio(win["close"].iloc[-1], df["close"].iloc[max(0, end - 20)]) - 1
    star_low = win["low"].min()
    gain_to_now = safe_ratio(df["close"].iloc[-1], star_low) - 1
    gain_after_15 = safe_ratio(after.head(15)["close"].max(), star_low) - 1
    launch_amount = safe_ratio(after.head(10)["amount"].max(), win["amount"].median())
    now_amount = safe_ratio(df["a5"].iloc[-1], df["a20"].iloc[-1])
    pct5 = safe_ratio(df["close"].iloc[-1], df["close"].iloc[-6]) - 1
    pct20 = safe_ratio(df["close"].iloc[-1], df["close"].iloc[-21]) - 1
    days_after = len(df) - 1 - end

    return {
        "start_idx": start,
        "end_idx": end,
        "start": win["date"].iloc[0].strftime("%Y-%m-%d"),
        "end": win["date"].iloc[-1].strftime("%Y-%m-%d"),
        "length": length,
        "pos": pos,
        "tight": tight,
        "amount_ratio": amount_ratio,
        "quiet_days": quiet_days,
        "pre_gain": pre_gain,
        "gain_to_now": gain_to_now,
        "gain_after_15": gain_after_15,
        "launch_amount": launch_amount,
        "now_amount": now_amount,
        "pct5": pct5,
        "pct20": pct20,
        "days_after": days_after,
    }


def base_star_filter(f):
    return (
        f["pos"] <= 0.62
        and f["tight"] <= 0.32
        and f["amount_ratio"] <= 0.82
        and f["quiet_days"] >= 0.52
        and f["pre_gain"] <= 0.30
    )


def sample_like_score(f, template):
    # Smaller distance means more like the reference sample. All features are shape ratios.
    weights = {
        "pos": 1.2,
        "tight": 1.3,
        "amount_ratio": 1.5,
        "quiet_days": 1.0,
        "launch_amount": 1.1,
        "gain_after_15": 0.9,
    }
    dist = 0.0
    for key, weight in weights.items():
        a = f[key]
        b = template[key]
        if key == "launch_amount":
            a = math.log1p(max(a, 0))
            b = math.log1p(max(b, 0))
        dist += weight * abs(a - b)
    return max(0.0, 100.0 - dist * 42.0)


def find_best_window(df, templates=None):
    best = None
    for length in range(8, 17):
        for start in range(max(70, len(df) - 135), len(df) - length - 3):
            f = feature_from_window(df, start, length)
            if not f or not base_star_filter(f):
                continue

            launched = (
                f["days_after"] <= 100
                and f["gain_to_now"] >= 0.18
                and ((f["gain_after_15"] >= 0.16 and f["launch_amount"] >= 2.0) or f["now_amount"] >= 1.45)
            )
            ready = (
                f["days_after"] <= 24
                and f["gain_to_now"] < 0.20
                and f["pct20"] <= 0.20
                and f["now_amount"] >= 0.85
            )
            if not launched and not ready:
                continue

            if templates:
                scores = [(sample_like_score(f, t), t["sample_name"]) for t in templates]
                similarity, sample_name = max(scores, key=lambda x: x[0])
            else:
                similarity, sample_name = 100.0, "样本"

            if similarity < 62:
                continue

            score = similarity + f["gain_to_now"] * 45 + max(0, 0.32 - f["tight"]) * 45
            score += max(0, 0.82 - f["amount_ratio"]) * 28
            f = dict(f)
            f["group"] = "已暴涨验证组" if launched else "有暴涨趋势组"
            f["similarity"] = similarity
            f["sample_name"] = sample_name
            f["score"] = score
            if best is None or f["score"] > best["score"]:
                best = f
    return best


def build_templates():
    templates = []
    lines = []
    for code, name in SAMPLES:
        try:
            df = prepare(fetch_history(code))
            f = find_best_window(df, templates=None)
            if f:
                f = dict(f)
                f["sample_code"] = code
                f["sample_name"] = name
                templates.append(f)
                lines.append(f"- {name}({code})：命中，星星量区 {f['start']} 至 {f['end']}，后涨幅 {f['gain_to_now']:.1%}")
            else:
                lines.append(f"- {name}({code})：未命中")
        except Exception as exc:
            lines.append(f"- {name}({code})：数据失败 {exc}")
        time.sleep(0.05)
    return templates, lines


def get_stock_list():
    try:
        d = ak.stock_zh_a_spot_em()
        d = d.rename(columns={"代码": "code", "名称": "name", "最新价": "price", "总市值": "cap"})
        d["code"] = d["code"].astype(str).str.zfill(6)
        d["name"] = d["name"].astype(str)
        d["price"] = pd.to_numeric(d["price"], errors="coerce")
        d["cap_yi"] = pd.to_numeric(d["cap"], errors="coerce") / 100000000
        d = d[~d["name"].str.contains("ST", case=False, na=False)]
        d = d[~d["code"].str.startswith(("8", "4", "9"))]
        d = d[(d["price"] >= 2.5) & (d["price"] <= 180)]
        d = d[(d["cap_yi"].isna()) | ((d["cap_yi"] >= 8) & (d["cap_yi"] <= 3000))]
        return d[["code", "name"]].drop_duplicates("code").head(MAX_STOCKS).to_dict("records")
    except Exception as exc:
        print("股票列表失败，使用代码段候选池：", exc)

    rows = []
    for start, end in [(1, 2000), (2001, 4000), (300001, 302000), (600000, 604000), (605000, 606000), (688001, 689000)]:
        for num in range(start, end):
            rows.append({"code": f"{num:06d}", "name": f"{num:06d}"})
            if len(rows) >= MAX_STOCKS:
                return rows
    return rows


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


def scan_item(item, templates):
    code = item["code"]
    name = item["name"]
    try:
        df = prepare(fetch_history(code))
        f = find_best_window(df, templates)
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


def format_item(x, idx):
    return (
        f"{idx}. {x['name']}({x['code']}) 收{x['close']:.2f}，评分{x['score']:.1f}，相似{x['sample_name']} {x['similarity']:.0f}%\n"
        f"   星星量区：{x['start']} 至 {x['end']}，后涨幅：{x['gain_to_now']:.1%}，5日：{x['pct5']:.1%}，20日：{x['pct20']:.1%}\n"
        f"   形态：成交额比{x['amount_ratio']:.2f}，振幅{x['tight']:.1%}，低位{x['pos']:.1%}，启动放量{x['launch_amount']:.1f}倍"
    )


def main():
    templates, sample_lines = build_templates()
    if not templates:
        raise RuntimeError("样本模板没有建立成功，停止扫描")
    items, results = scan_market(templates)
    exploded = sorted([x for x in results if x["group"] == "已暴涨验证组"], key=lambda x: x["score"], reverse=True)[:30]
    ready = sorted([x for x in results if x["group"] == "有暴涨趋势组"], key=lambda x: x["score"], reverse=True)[:30]

    title = "AKI星星量相似度筛选 " + dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# {title}", "", "说明：样本只建模板，正式筛选已排除样本股。按成交额形态相似度筛选，不是买卖建议。", ""]
    lines.append("## 样本模板验证")
    lines.extend(sample_lines)
    lines.append("")
    for group, rows in [("正式筛选：已暴涨验证组", exploded), ("正式筛选：有暴涨趋势组", ready)]:
        lines.append(f"## {group}（{len(rows)}只）")
        lines.extend(["今天没有命中。"] if not rows else [format_item(x, i) for i, x in enumerate(rows, 1)])
        lines.append("")
    lines.append(f"正式扫描 {len(items)} 只，命中 {len(results)} 只。")
    text = "\n".join(lines)
    print(text)
    push_server_chan(title, text)


if __name__ == "__main__":
    main()
