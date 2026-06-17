# -*- coding: utf-8 -*-
"""
星星量筛选器 v4 —— 盯盘清单 + 已暴涨累积档案
===========================================
每天推送：
  🌱 蓄势待发：巨量已现、价格还没动（重点）
  ⭐ 今日新加盯盘：今天刚放巨量、记进清单的票（会列出是哪几只）
  🎯 今日新命中：盯盘里的票启动了（我们提前蹲到的，打⭐）
  ✅ 已暴涨档案：累积记录，命中的标⭐，方便长期观察战绩

两个记忆文件（提交回仓库）：
  watchlist.json —— 蓄势盯盘清单
  archive.json   —— 已暴涨累积档案（来源 hit=我们预判 / market=全市场扫到）

数据源：东方财富。推送：Server酱(SERVERCHAN_SENDKEY)。
说明：机械筛选，命中≠买入建议。"量在前价在后"不确定性高，请自行复核。
"""

import os
import json
import time
import datetime as dt

import requests
import pandas as pd
import akshare as ak


CONFIG = {
    # 候选池
    "pool_turnover_min": 5.0,
    "max_candidates": 800,
    "price_min": 2.0,
    "price_max": 200.0,
    "mktcap_min_yi": 20.0,
    "exclude_kechuang_beijing": True,

    # 巨量/底部
    "hist_days": 150,
    "baseline_window": 60,
    "vol_mult": 2.5,
    "surge_window": 3,
    "low_lookback": 60,
    "low_recent_days": 25,
    "flag_rise_max": 18.0,      # 加入盯盘时离底涨幅上限

    # 蓄势待发
    "watch_min_age": 1,
    "watch_max_age": 12,
    "today_calm_max": 4.0,
    "flat_rise_max": 12.0,
    "post_spike_band": 8.0,
    "collapse_drop": 10.0,
    "quiet_vol_mult": 2.0,

    # 命中 / 已暴涨
    "launched_rise": 20.0,     # 盯盘票离底涨到此=启动/命中
    "surged_rise_min": 35.0,   # 全市场已暴涨门槛（离底涨幅）

    # 运行
    "request_sleep": 0.2,
    "retries": 3,
    "max_push_each": 30,
    "max_archive_push": 40,
    "watchlist_file": "watchlist.json",
    "archive_file": "archive.json",
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


def load_json(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_json(path, obj):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print("保存失败：", path, e)


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


def upsert_archive(archive, code, name, rise, close, today, source, flagged_date=None):
    if code in archive:
        a = archive[code]
        a["peak_rise_from_low"] = max(a.get("peak_rise_from_low", 0), round(rise, 1))
        a["last_close"] = round(close, 2)
        a["last_update"] = today
        if source == "hit" and a.get("source") != "hit":
            a["source"] = "hit"
            a["flagged_date"] = flagged_date
    else:
        archive[code] = {"name": name, "first_date": today, "source": source,
                         "flagged_date": flagged_date,
                         "first_rise_from_low": round(rise, 1),
                         "peak_rise_from_low": round(rise, 1),
                         "last_close": round(close, 2), "last_update": today}


# ---------------- 复查盯盘清单 ----------------
def recheck_watchlist(wl, archive, today):
    sprout, hits, new_wl = [], [], {}
    for code, info in wl.items():
        k = get_kline(code)
        time.sleep(CONFIG["request_sleep"])
        if k is None:
            new_wl[code] = info
            continue
        dates = k["date"].astype(str).tolist()
        sd = info.get("spike_date")
        if sd not in dates:
            continue
        idx = dates.index(sd)
        age = (len(k) - 1) - idx
        if age > CONFIG["watch_max_age"]:
            continue
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
               "age": age, "ratio": round(spike_ratio, 2),
               "flagged": info.get("spike_date")}

        if rise_from_low >= CONFIG["launched_rise"]:
            upsert_archive(archive, code, info.get("name", ""), rise_from_low,
                           today_close, today, "hit", info.get("spike_date"))
            hits.append(row)
            continue
        if post_spike <= -CONFIG["collapse_drop"]:
            continue
        new_wl[code] = info
        if (age >= CONFIG["watch_min_age"]
                and abs(today_pct) <= CONFIG["today_calm_max"]
                and rise_from_low <= CONFIG["flat_rise_max"]
                and abs(post_spike) <= CONFIG["post_spike_band"]
                and today_vol <= base * CONFIG["quiet_vol_mult"]):
            sprout.append(row)
    return sprout, hits, new_wl


# ---------------- 扫今日：新巨量 + 已暴涨 ----------------
def scan_pool(wl, archive, today):
    pool = get_candidate_pool()
    print(f"今日候选池：{len(pool)} 只")
    new_watch, surged = [], []
    sw = CONFIG["surge_window"]
    for _, r in pool.head(CONFIG["max_candidates"]).iterrows():
        code = str(r["code"])
        k = get_kline(code)
        time.sleep(CONFIG["request_sleep"])
        if k is None:
            continue
        close, vol = k["close"], k["vol"]
        today_close = float(close.iloc[-1])
        today_pct = float(k["pct"].iloc[-1]) if "pct" in k else 0.0
        at_bottom, stage_low = near_bottom(close)
        rise_from_low = (today_close / stage_low - 1) * 100

        # 已暴涨：离底大 + 近期有巨量 + 站上MA20
        base_em = baseline_before(vol, len(vol) - sw) or 1
        ratio_em = vol.iloc[-sw:].max() / base_em
        ma20 = close.rolling(20).mean().iloc[-1]
        if (rise_from_low >= CONFIG["surged_rise_min"] and ratio_em >= CONFIG["vol_mult"]
                and not pd.isna(ma20) and today_close > ma20):
            src = archive.get(code, {}).get("source", "market")
            upsert_archive(archive, code, r.get("name", ""), rise_from_low,
                           today_close, today, src)
            surged.append({"code": code, "name": r.get("name", ""),
                           "rise_from_low": round(rise_from_low, 1)})

        # 新巨量加盯盘（今天放巨量、在底部、还没涨上去；且未在清单/档案）
        if code in wl or code in archive:
            continue
        base = baseline_before(vol, len(vol) - 1)
        if not base or base <= 0:
            continue
        if (vol.iloc[-1] / base >= CONFIG["vol_mult"] and at_bottom
                and rise_from_low <= CONFIG["flag_rise_max"]):
            wl[code] = {"name": r.get("name", ""), "added": today,
                        "spike_date": str(k["date"].iloc[-1]),
                        "spike_close": round(today_close, 2)}
            new_watch.append({"code": code, "name": r.get("name", ""),
                              "close": round(today_close, 2), "pct": round(today_pct, 1),
                              "rise_from_low": round(rise_from_low, 1),
                              "ratio": round(vol.iloc[-1] / base, 2)})
    return new_watch, surged


def make_table(items, head, cols):
    sep = "| " + " | ".join(["---"] * len(cols)) + " |"
    lines = [head, "| " + " | ".join(c[0] for c in cols) + " |", sep]
    for h in items:
        lines.append("| " + " | ".join(str(c[1](h)) for c in cols) + " |")
    return "\n".join(lines)


# ---------------- 主流程 ----------------
def main():
    today = dt.date.today().strftime("%Y-%m-%d")
    print(f"=== 星星量 v4 {today} ===")

    wl = load_json(CONFIG["watchlist_file"])
    archive = load_json(CONFIG["archive_file"])
    print(f"载入：盯盘 {len(wl)} 只，档案 {len(archive)} 只")

    sprout, hits, wl = recheck_watchlist(wl, archive, today)

    new_watch, surged, pool_err = [], [], None
    try:
        new_watch, surged = scan_pool(wl, archive, today)
    except Exception as e:
        pool_err = str(e)

    save_json(CONFIG["watchlist_file"], wl)
    save_json(CONFIG["archive_file"], archive)

    parts = []

    # 🌱 蓄势待发
    if sprout:
        sprout.sort(key=lambda x: x["ratio"], reverse=True)
        parts.append(make_table(
            sprout[: CONFIG["max_push_each"]],
            f"#### 🌱 蓄势待发（巨量已现、还没动）— {len(sprout)}只\n",
            [("代码", lambda h: h["code"]), ("名称", lambda h: h["name"]),
             ("现价", lambda h: h["close"]), ("今日", lambda h: f"{h['pct']}%"),
             ("离底", lambda h: f"{h['rise_from_low']}%"),
             ("巨量倍数", lambda h: f"{h['ratio']}x"),
             ("巨量在", lambda h: f"{h['age']}天前")]))
    else:
        parts.append("#### 🌱 蓄势待发 — 今日 0 只")

    # ⭐ 今日新加盯盘（哪几只）
    if new_watch:
        new_watch.sort(key=lambda x: x["ratio"], reverse=True)
        parts.append(make_table(
            new_watch[: CONFIG["max_push_each"]],
            f"\n#### ⭐ 今日新加盯盘（刚放巨量、记入清单）— {len(new_watch)}只\n",
            [("代码", lambda h: h["code"]), ("名称", lambda h: h["name"]),
             ("现价", lambda h: h["close"]), ("今日", lambda h: f"{h['pct']}%"),
             ("离底", lambda h: f"{h['rise_from_low']}%"),
             ("巨量倍数", lambda h: f"{h['ratio']}x")]))

    # 🎯 今日新命中
    if hits:
        hits.sort(key=lambda x: x["rise_from_low"], reverse=True)
        parts.append(make_table(
            hits[: CONFIG["max_push_each"]],
            f"\n#### 🎯 今日新命中（提前蹲到的票启动了）— {len(hits)}只\n",
            [("代码", lambda h: h["code"]), ("名称", lambda h: h["name"]),
             ("现价", lambda h: h["close"]), ("离底", lambda h: f"{h['rise_from_low']}%"),
             ("巨量在", lambda h: f"{h['age']}天前发现")]))

    # ✅ 已暴涨档案（累积，命中标⭐）
    hit_cnt = sum(1 for a in archive.values() if a.get("source") == "hit")
    if archive:
        items = sorted(archive.items(),
                       key=lambda kv: (kv[1].get("source") != "hit",
                                       -kv[1].get("peak_rise_from_low", 0)))
        rows = [{"code": c, "name": a.get("name", ""),
                 "peak": a.get("peak_rise_from_low", 0),
                 "mark": "⭐命中" if a.get("source") == "hit" else "普通",
                 "first": a.get("first_date", "")}
                for c, a in items[: CONFIG["max_archive_push"]]]
        parts.append(make_table(
            rows,
            f"\n#### ✅ 已暴涨档案 — 累计{len(archive)}只（其中⭐命中{hit_cnt}只，今日新进{len(surged)}）\n",
            [("代码", lambda h: h["code"]), ("名称", lambda h: h["name"]),
             ("最高离底", lambda h: f"{h['peak']}%"),
             ("来源", lambda h: h["mark"]), ("首记", lambda h: h["first"])]))
    else:
        parts.append("\n#### ✅ 已暴涨档案 — 暂无")

    foot = f"\n> 盯盘池 {len(wl)} 只。"
    if pool_err:
        foot += f"（榜单接口异常：{pool_err}，今日未扫新增）"
    foot += "\n> 机械筛选，非投资建议。量在前价在后不确定性高，请自行复核。"
    parts.append(foot)

    title = (f"🌱星星量 {today}：蓄势{len(sprout)}/命中{len(hits)}/"
             f"已涨累计{len(archive)}")
    server_chan_push(title, "\n".join(parts))
    print(f"完成。蓄势{len(sprout)} 命中{len(hits)} 新盯盘{len(new_watch)} "
          f"已暴涨今日{len(surged)} 档案{len(archive)}")


if __name__ == "__main__":
    main()
