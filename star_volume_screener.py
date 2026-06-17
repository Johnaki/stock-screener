# -*- coding: utf-8 -*-
"""
星星量筛选器 v3 —— 盯盘清单版
==============================
目标：抓"巨量(星星量)已经出现、但价格还没启动"的蓄势票。

机制：
  1) 每天扫今日活跃股，发现"在底部突然放巨量"的票 -> 当天记进盯盘清单
     （巨量当天这只票本来就活跃，抓得到）。
  2) 每天复查清单里已有的票：
     🌱 蓄势待发：巨量已过去几天、价格还趴着没动、今天也安静 -> 推给你（重点）
     🚀 已启动 ：盯盘中的票开始涨了 -> 推一下做验证，然后移出清单
     ✖ 失效  ：放量后反而跌下去（疑似出货）或太久没动 -> 移出清单
  3) 清单存成 watchlist.json，提交回仓库，第二天接着用。

数据源：东方财富。推送：Server酱(SERVERCHAN_SENDKEY)。
说明：机械筛选，命中≠买入建议。"量在前价在后"本就不确定，假信号偏多，请自行复核。
"""

import os
import json
import time
import datetime as dt

import requests
import pandas as pd
import akshare as ak


CONFIG = {
    # 候选池（用于发现"今日新放巨量"）
    "pool_turnover_min": 5.0,
    "max_candidates": 800,
    "price_min": 2.0,
    "price_max": 200.0,
    "mktcap_min_yi": 20.0,
    "exclude_kechuang_beijing": True,

    # 形态/巨量
    "hist_days": 150,
    "baseline_window": 60,     # 巨量日之前多少日的均量做基准
    "vol_mult": 2.5,           # 巨量倍数：巨量日量 / 基准均量
    "low_lookback": 60,        # 阶段低点统计窗口
    "low_recent_days": 25,     # 低点须出现在最近多少日内（确认在底部）
    "flag_rise_max": 18.0,     # 加入盯盘时，离底涨幅上限（还在底部，没涨上去）

    # 🌱 蓄势待发（复查条件）
    "watch_min_age": 1,        # 巨量至少几个交易日前（>=1 即不含今天）
    "watch_max_age": 12,       # 巨量最多几个交易日前（超过=作废）
    "today_calm_max": 4.0,     # 今日涨跌幅绝对值上限（还很安静）
    "flat_rise_max": 12.0,     # 离底涨幅上限（还没拉起来）
    "post_spike_band": 8.0,    # 相对巨量日收盘的偏离上限（横住，没大涨大跌）
    "collapse_drop": 10.0,     # 相对巨量日跌超此值=疑似出货，剔除
    "quiet_vol_mult": 2.0,     # 今日量须 <= 基准均量 * 此值（确认缩量、安静）

    # 🚀 已启动（毕业）
    "launched_rise": 20.0,     # 离底涨幅超此=已启动

    # 运行
    "request_sleep": 0.2,
    "retries": 3,
    "max_push_each": 30,
    "watchlist_file": "watchlist.json",
}

SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
}
EM_HOSTS = ["push2.eastmoney.com", "1.push2.eastmoney.com",
            "7.push2.eastmoney.com", "13.push2.eastmoney.com",
            "82.push2.eastmoney.com"]


# ---------------- 工具 ----------------
def safe_call(func, *args, retries=None, base_sleep=1.0, **kwargs):
    if retries is None:
        retries = CONFIG["retries"]
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt == retries - 1:
                print(f"  接口失败({getattr(func,'__name__',func)}): {e}")
                return None
            time.sleep(base_sleep * (attempt + 1))
    return None


def to_num(v):
    try:
        if v in ("-", "", None):
            return None
        return float(v)
    except Exception:
        return None


def server_chan_push(title, desp):
    if not SENDKEY:
        print("⚠️ 未设置 SERVERCHAN_SENDKEY，只打印：\n", title, "\n", desp)
        return
    try:
        r = requests.post(f"https://sctapi.ftqq.com/{SENDKEY}.send",
                          data={"title": title, "desp": desp}, timeout=20)
        print("推送结果：", r.status_code, r.text[:200])
    except Exception as e:
        print("推送失败：", e)


def load_watchlist():
    path = CONFIG["watchlist_file"]
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_watchlist(wl):
    try:
        with open(CONFIG["watchlist_file"], "w", encoding="utf-8") as f:
            json.dump(wl, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print("保存清单失败：", e)


# ---------------- 取数 ----------------
def fetch_clist_page(pn, pz=200, fid="f8"):
    params = {
        "pn": pn, "pz": pz, "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2", "fid": fid,
        "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23",
        "fields": "f2,f3,f8,f10,f12,f14,f20",
        "_": str(int(time.time() * 1000)),
    }
    for host in EM_HOSTS:
        try:
            r = requests.get(f"https://{host}/api/qt/clist/get",
                             params=params, headers=HEADERS, timeout=15)
            diff = (r.json() or {}).get("data", {}).get("diff")
            if diff:
                return diff
        except Exception:
            continue
    return None


def get_candidate_pool():
    rows, pn = [], 1
    while len(rows) < CONFIG["max_candidates"]:
        diff = None
        for _ in range(CONFIG["retries"]):
            diff = fetch_clist_page(pn)
            if diff:
                break
            time.sleep(1.0)
        if not diff:
            if pn == 1:
                raise RuntimeError("榜单接口连不上（多个域名都失败）")
            break
        stop = False
        for d in diff:
            turn = to_num(d.get("f8"))
            if turn is None:
                continue
            if turn < CONFIG["pool_turnover_min"]:
                stop = True
                break
            rows.append({"code": str(d.get("f12")), "name": d.get("f14"),
                         "price": to_num(d.get("f2")), "mktcap": to_num(d.get("f20"))})
        if stop:
            break
        pn += 1
        time.sleep(0.2)

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df[df["name"].astype(str).str.contains("ST|退") == False]  # noqa
    df = df[df["price"].notna() & (df["price"] >= CONFIG["price_min"])
            & (df["price"] <= CONFIG["price_max"])]
    if CONFIG["exclude_kechuang_beijing"]:
        df = df[~df["code"].astype(str).str.startswith(("688", "8", "4", "9"))]
    if CONFIG["mktcap_min_yi"] > 0:
        df = df[df["mktcap"].notna() & (df["mktcap"] >= CONFIG["mktcap_min_yi"] * 1e8)]
    return df.reset_index(drop=True)


def get_kline(code):
    end = dt.date.today().strftime("%Y%m%d")
    start = (dt.date.today() - dt.timedelta(days=CONFIG["hist_days"])).strftime("%Y%m%d")
    k = safe_call(ak.stock_zh_a_hist, symbol=code, period="daily",
                  start_date=start, end_date=end, adjust="qfq", timeout=20)
    if k is None or len(k) < CONFIG["baseline_window"] + 5:
        return None
    k = k.rename(columns={"收盘": "close", "成交量": "vol", "最高": "high",
                          "最低": "low", "换手率": "turn", "涨跌幅": "pct", "日期": "date"})
    for c in ["close", "vol", "high", "low", "pct"]:
        if c in k:
            k[c] = pd.to_numeric(k[c], errors="coerce")
    k = k.dropna(subset=["close", "vol"]).reset_index(drop=True)
    return k if len(k) >= CONFIG["baseline_window"] + 5 else None


def baseline_before(vol, idx):
    lo = max(0, idx - CONFIG["baseline_window"])
    seg = vol.iloc[lo:idx]
    return seg.mean() if len(seg) > 0 else None


def near_bottom(close):
    lb = CONFIG["low_lookback"]
    seg = close.iloc[-lb:] if len(close) >= lb else close
    low_pos = len(seg) - 1 - int(seg.values.argmin())
    return low_pos <= CONFIG["low_recent_days"], seg.min()


# ---------------- 主流程 ----------------
def recheck_watchlist(wl, today):
    """复查清单：返回 (蓄势列表, 已启动列表, 新清单)"""
    sprout, launched, new_wl = [], [], {}
    for code, info in wl.items():
        k = get_kline(code)
        time.sleep(CONFIG["request_sleep"])
        if k is None:
            new_wl[code] = info  # 临时拉不到，先留着
            continue
        dates = k["date"].astype(str).tolist()
        sd = info.get("spike_date")
        if sd not in dates:
            continue  # 巨量日已不在窗口内，作废
        idx = dates.index(sd)
        age = (len(k) - 1) - idx
        if age > CONFIG["watch_max_age"]:
            continue  # 太久没动，作废
        close = k["close"]
        today_close = float(close.iloc[-1])
        spike_close = float(close.iloc[idx])
        _, stage_low = near_bottom(close)
        rise_from_low = (today_close / stage_low - 1) * 100
        today_pct = float(k["pct"].iloc[-1]) if "pct" in k else 0.0
        post_spike = (today_close / spike_close - 1) * 100
        base = baseline_before(k["vol"], idx) or 1
        spike_ratio = float(k["vol"].iloc[idx]) / base
        today_vol = float(k["vol"].iloc[-1])
        row = {"code": code, "name": info.get("name", ""), "close": round(today_close, 2),
               "pct": round(today_pct, 1), "rise_from_low": round(rise_from_low, 1),
               "age": age, "ratio": round(spike_ratio, 2)}

        if rise_from_low >= CONFIG["launched_rise"]:
            launched.append(row)              # 毕业：开始涨了
            continue
        if post_spike <= -CONFIG["collapse_drop"]:
            continue                           # 放量后跌穿，疑似出货，剔除
        new_wl[code] = info                    # 仍在窗口，继续盯
        if (age >= CONFIG["watch_min_age"]
                and abs(today_pct) <= CONFIG["today_calm_max"]
                and rise_from_low <= CONFIG["flat_rise_max"]
                and abs(post_spike) <= CONFIG["post_spike_band"]
                and today_vol <= base * CONFIG["quiet_vol_mult"]):
            sprout.append(row)                 # 蓄势待发
    return sprout, launched, new_wl


def scan_new_spikes(wl, today):
    """扫今日活跃股，发现新巨量，加入清单。返回新增数。"""
    try:
        pool = get_candidate_pool()
    except Exception as e:
        raise e
    print(f"今日候选池：{len(pool)} 只")
    added = 0
    for _, r in pool.head(CONFIG["max_candidates"]).iterrows():
        code = str(r["code"])
        if code in wl:
            continue
        k = get_kline(code)
        time.sleep(CONFIG["request_sleep"])
        if k is None:
            continue
        idx = len(k) - 1
        base = baseline_before(k["vol"], idx)
        if not base or base <= 0:
            continue
        if k["vol"].iloc[idx] / base < CONFIG["vol_mult"]:
            continue                            # 今日不是巨量
        at_bottom, stage_low = near_bottom(k["close"])
        if not at_bottom:
            continue
        rise_from_low = (float(k["close"].iloc[-1]) / stage_low - 1) * 100
        if rise_from_low > CONFIG["flag_rise_max"]:
            continue                            # 已经涨上去了，不是底部新放量
        wl[code] = {"name": r.get("name", ""),
                    "added": today,
                    "spike_date": str(k["date"].iloc[-1]),
                    "spike_close": round(float(k["close"].iloc[-1]), 2)}
        added += 1
    return added


def make_table(items, head, cols):
    sep = "| " + " | ".join(["---"] * len(cols)) + " |"
    lines = [head, "| " + " | ".join(c[0] for c in cols) + " |", sep]
    for h in items:
        lines.append("| " + " | ".join(str(c[1](h)) for c in cols) + " |")
    return "\n".join(lines)


def main():
    today = dt.date.today().strftime("%Y-%m-%d")
    print(f"=== 星星量 v3 {today} ===")

    wl = load_watchlist()
    print(f"载入盯盘清单：{len(wl)} 只")

    # 先复查（不依赖榜单接口）
    sprout, launched, wl = recheck_watchlist(wl, today)

    # 再扫今日新巨量加入清单
    added = 0
    pool_err = None
    try:
        added = scan_new_spikes(wl, today)
    except Exception as e:
        pool_err = str(e)

    save_watchlist(wl)

    # 组织推送
    parts = []
    if sprout:
        sprout.sort(key=lambda x: x["ratio"], reverse=True)
        parts.append(make_table(
            sprout[: CONFIG["max_push_each"]],
            f"#### 🌱 蓄势待发（巨量已现、价格还没动）— {len(sprout)}只\n",
            [("代码", lambda h: h["code"]), ("名称", lambda h: h["name"]),
             ("现价", lambda h: h["close"]), ("今日", lambda h: f"{h['pct']}%"),
             ("离底", lambda h: f"{h['rise_from_low']}%"),
             ("巨量倍数", lambda h: f"{h['ratio']}x"),
             ("巨量在", lambda h: f"{h['age']}天前")]))
    else:
        parts.append("#### 🌱 蓄势待发 — 今日 0 只")

    if launched:
        launched.sort(key=lambda x: x["rise_from_low"], reverse=True)
        parts.append(make_table(
            launched[: CONFIG["max_push_each"]],
            f"\n#### 🚀 已启动（盯盘中转涨，验证用）— {len(launched)}只\n",
            [("代码", lambda h: h["code"]), ("名称", lambda h: h["name"]),
             ("现价", lambda h: h["close"]), ("离底", lambda h: f"{h['rise_from_low']}%")]))

    foot = f"\n> 盯盘池 {len(wl)} 只，今日新增 {added} 只。"
    if pool_err:
        foot += f"（榜单接口异常：{pool_err}，今日未新增）"
    foot += "\n> 机械筛选，非投资建议。量在前价在后不确定性高，请自行复核。"
    parts.append(foot)

    title = f"🌱星星量 {today}：蓄势{len(sprout)} / 启动{len(launched)}"
    server_chan_push(title, "\n".join(parts))
    print(f"完成。蓄势{len(sprout)} 启动{len(launched)} 新增{added} 清单{len(wl)}")


if __name__ == "__main__":
    main()
