# -*- coding: utf-8 -*-
"""
地板股筛选器 v5.0 —— 长期地量横盘（地板形态）+ 突破命中 + 多时段重试
=====================================================================
形态定义（地板/地量横盘，俗称"星星量"——量小如星点）：
  · 股价低位横盘，贴着地板走，不怎么动
  · 成交量全程地量，没有放量大单根
  · 换手率持续 < 4%
  · 这种形态持续 ≥ 30 个交易日
  · 蹲够了，往往随后放量突破/暴涨

每天推送：
  🌱 蓄势待发：当前还蹲在地板里、没启动的票（重点，要找的就是这些）
  🎯 今日突破：之前蹲到的地板股，现在放量突破了（命中）
  ✅ 已兑现档案：地板→突破的累积战绩（命中⭐）

记忆文件（提交回仓库）：watchlist.json（盯盘地板股）/ archive.json（已兑现）/ state.json
数据源：东方财富。推送：Server酱(SERVERCHAN_SENDKEY)。
工作流一天跑多个时间点：只在榜单连上、扫到数据那次推送；连不上静默；当天成功后跳过。
说明：机械筛选，非投资建议。地量横盘后不一定突破，请自行复核。
"""

import os
import json
import time
import datetime as dt

import requests
import pandas as pd
import akshare as ak


CONFIG = {
    # 候选池：要"安静"的票（今日换手率低）
    "pool_turnover_min": 0.3,    # 排除停牌/近乎零成交
    "pool_turnover_max": 4.0,    # 只看今日换手率 < 4% 的（地量候选）
    "max_candidates": 2000,      # 最多检查多少只（地量股很多，按今日最安静的往下取）
    "price_min": 2.0,
    "price_max": 200.0,
    "mktcap_min_yi": 20.0,
    "exclude_kechuang_beijing": True,

    # 地板形态（地量横盘）
    "hist_days": 240,            # 拉够历史：要看30天地板 + 半年底部位置
    "floor_days": 30,            # 地板至少持续多少个交易日
    "floor_turnover_max": 4.0,   # 地板期换手率上限（基本每天都要 < 此值）
    "floor_turnover_exceed_allow": 2,  # 30天里容许几天超过上限（防个别噪声）
    "floor_turnover_min_avg": 0.2,     # 平均换手率下限（排除停牌/僵尸）
    "floor_range_max": 25.0,     # 地板期振幅上限(%)：(区间高-区间低)/区间低
    "bottom_lookback": 120,      # 判断"是否在底部"的长周期
    "bottom_tol": 35.0,          # 现价离半年最低点不超过此(%)，才算"地板/低位"

    # 突破 / 命中
    "breakout_rise": 20.0,       # 突破地板上沿此幅度=启动/命中
    "breakdown_drop": 15.0,      # 跌破地板下沿此幅度=失败，剔除

    # 运行
    "request_sleep": 0.2,
    "retries": 3,
    "max_push_each": 40,
    "max_archive_push": 50,
    "watchlist_file": "watchlist.json",
    "archive_file": "archive.json",
    "state_file": "state.json",
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
def fetch_clist_page(pn, pz=200, fid="f8", po="0"):
    # po=0 升序（最安静在前）；fid=f8 换手率
    params = {
        "pn": pn, "pz": pz, "po": po, "np": "1",
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
    """取今日最安静（换手率最低）的一批票，作为地板候选。升序翻页，
    收集 0.3% < 换手率 < 4% 的票，直到达到上限或越过 4%。"""
    rows, pn = [], 1
    while len(rows) < CONFIG["max_candidates"]:
        diff = None
        for _ in range(CONFIG["retries"]):
            diff = fetch_clist_page(pn, po="0")
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
            if turn >= CONFIG["pool_turnover_max"]:   # 升序，越过上限就停
                stop = True
                break
            if turn < CONFIG["pool_turnover_min"]:     # 太低（停牌/僵尸）跳过
                continue
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
    need = CONFIG["floor_days"] + 10
    if k is None or len(k) < need:
        return None
    k = k.rename(columns={"收盘": "close", "成交量": "vol", "最高": "high",
                          "最低": "low", "换手率": "turn", "涨跌幅": "pct", "日期": "date"})
    for c in ["close", "vol", "high", "low", "turn", "pct"]:
        if c in k:
            k[c] = pd.to_numeric(k[c], errors="coerce")
    k = k.dropna(subset=["close"]).reset_index(drop=True)
    return k if len(k) >= need else None


# ---------------- 地板形态判定 ----------------
def detect_floor(k):
    """判断该股当前是否处在'地量横盘地板'里。返回 (是否, info)。"""
    n = len(k)
    fd = CONFIG["floor_days"]
    if n < fd + 5 or "turn" not in k:
        return False, None
    close, high, low, turn = k["close"], k["high"], k["low"], k["turn"]
    seg_turn = turn.iloc[-fd:]
    seg_high = high.iloc[-fd:]
    seg_low = low.iloc[-fd:]

    # 1) 换手率：地板期内基本都要 < 上限（容许极少数噪声天）
    exceed = int(((seg_turn >= CONFIG["floor_turnover_max"]) | seg_turn.isna()).sum())
    if exceed > CONFIG["floor_turnover_exceed_allow"]:
        return False, None
    avg_turn = float(seg_turn.mean())
    if pd.isna(avg_turn) or avg_turn < CONFIG["floor_turnover_min_avg"]:
        return False, None

    # 2) 横盘：地板期振幅 ≤ 上限
    p_hi = float(seg_high.max())
    p_lo = float(seg_low.min())
    if p_lo <= 0:
        return False, None
    rng = (p_hi / p_lo - 1) * 100
    if rng > CONFIG["floor_range_max"]:
        return False, None

    # 3) 低位/地板：现价离半年最低点不超过 bottom_tol
    bl = CONFIG["bottom_lookback"]
    seg_b = low.iloc[-bl:] if n >= bl else low
    bottom = float(seg_b.min())
    today_close = float(close.iloc[-1])
    if bottom <= 0:
        return False, None
    above_bottom = (today_close / bottom - 1) * 100
    if above_bottom > CONFIG["bottom_tol"]:
        return False, None

    # 已蹲天数：从今天往回数，连续换手率 < 上限 的天数
    floor_len = 0
    for t in reversed(turn.tolist()):
        if pd.notna(t) and t < CONFIG["floor_turnover_max"]:
            floor_len += 1
        else:
            break

    info = {
        "today_close": round(today_close, 2),
        "floor_len": floor_len,
        "avg_turn": round(avg_turn, 2),
        "max_turn": round(float(seg_turn.max()), 2),
        "range": round(rng, 1),
        "above_bottom": round(above_bottom, 1),
        "ref_high": round(p_hi, 2),
        "ref_low": round(p_lo, 2),
    }
    return True, info


def upsert_archive(archive, code, name, rise, close, today, source, flagged_date=None):
    if code in archive:
        a = archive[code]
        a["peak_rise"] = max(a.get("peak_rise", 0), round(rise, 1))
        a["last_close"] = round(close, 2)
        a["last_update"] = today
    else:
        archive[code] = {"name": name, "first_date": today, "source": source,
                         "flagged_date": flagged_date,
                         "first_rise": round(rise, 1), "peak_rise": round(rise, 1),
                         "last_close": round(close, 2), "last_update": today}


# ---------------- 扫地板（蓄势）----------------
def scan_floors(pool, wl, today):
    floors, floor_codes = [], set()
    n = len(pool)
    for i, r in pool.iterrows():
        code = str(r["code"])
        k = get_kline(code)
        time.sleep(CONFIG["request_sleep"])
        if k is None:
            continue
        ok, info = detect_floor(k)
        if not ok:
            continue
        floor_codes.add(code)
        if code not in wl:
            wl[code] = {"name": r.get("name", ""), "flagged_date": today,
                        "ref_high": info["ref_high"], "ref_low": info["ref_low"]}
        else:
            wl[code]["last_seen"] = today
        info["code"] = code
        info["name"] = r.get("name", "")
        info["flagged_date"] = wl[code].get("flagged_date", today)
        floors.append(info)
        if (i + 1) % 200 == 0:
            print(f"  ...已扫 {i+1}/{n}，命中地板 {len(floors)}")
    return floors, floor_codes


# ---------------- 复查盯盘：突破=命中 / 跌破=剔除 ----------------
def recheck_watchlist(wl, archive, floor_codes, today):
    hits, extra_floors, drop = [], [], []
    for code, info in list(wl.items()):
        if code in floor_codes:
            continue  # 今天还在地板里，已经在蓄势里了，跳过
        k = get_kline(code)
        time.sleep(CONFIG["request_sleep"])
        if k is None:
            continue  # 取不到，先留着
        today_close = float(k["close"].iloc[-1])
        ref_high = info.get("ref_high", today_close)
        ref_low = info.get("ref_low", today_close)
        rise = (today_close / ref_high - 1) * 100 if ref_high else 0.0

        if rise >= CONFIG["breakout_rise"]:
            upsert_archive(archive, code, info.get("name", ""), rise, today_close,
                           today, "hit", info.get("flagged_date"))
            hits.append({"code": code, "name": info.get("name", ""),
                         "close": round(today_close, 2), "rise": round(rise, 1),
                         "flagged": info.get("flagged_date", "")})
            drop.append(code)
            continue
        if ref_low and today_close < ref_low * (1 - CONFIG["breakdown_drop"] / 100):
            drop.append(code)  # 跌破地板，失败
            continue
        # 还没在池里被扫到，但可能仍在蹲：再判一次
        ok, finfo = detect_floor(k)
        if ok:
            finfo["code"] = code
            finfo["name"] = info.get("name", "")
            finfo["flagged_date"] = info.get("flagged_date", today)
            extra_floors.append(finfo)
        # 既没突破也没跌破、也不算地板（突破途中）→ 留着继续看
    for c in drop:
        wl.pop(c, None)
    return hits, extra_floors


def make_table(items, head, cols):
    sep = "| " + " | ".join(["---"] * len(cols)) + " |"
    lines = [head, "| " + " | ".join(c[0] for c in cols) + " |", sep]
    for h in items:
        lines.append("| " + " | ".join(str(c[1](h)) for c in cols) + " |")
    return "\n".join(lines)


# ---------------- 主流程 ----------------
def main():
    today = dt.date.today().strftime("%Y-%m-%d")
    print(f"=== 地板股 v5.0 {today} ===")

    state = load_json(CONFIG["state_file"])
    if state.get("last_push_date") == today:
        print("今天已成功推送过，跳过本次运行。")
        return

    try:
        pool = get_candidate_pool()
    except Exception as e:
        print(f"榜单接口连不上（{e}），本次静默跳过，等下个时间点重试。")
        return
    if pool.empty:
        print("候选池为空，静默跳过。")
        return

    wl = load_json(CONFIG["watchlist_file"])
    archive = load_json(CONFIG["archive_file"])
    print(f"载入：盯盘 {len(wl)} 只，档案 {len(archive)} 只；候选池 {len(pool)} 只（开始逐只扫地板，较慢）")

    floors, floor_codes = scan_floors(pool, wl, today)
    hits, extra_floors = recheck_watchlist(wl, archive, floor_codes, today)
    floors.extend(extra_floors)

    save_json(CONFIG["watchlist_file"], wl)
    save_json(CONFIG["archive_file"], archive)

    parts = []

    # 🌱 蓄势待发（还蹲在地板里）
    if floors:
        floors.sort(key=lambda x: x.get("floor_len", 0), reverse=True)
        parts.append(make_table(
            floors[: CONFIG["max_push_each"]],
            f"#### 🌱 蓄势待发（地量横盘、还在蹲）— {len(floors)}只\n",
            [("代码", lambda h: h["code"]), ("名称", lambda h: h["name"]),
             ("现价", lambda h: h["today_close"]),
             ("已蹲", lambda h: f"{h['floor_len']}天"),
             ("振幅", lambda h: f"{h['range']}%"),
             ("离底", lambda h: f"{h['above_bottom']}%"),
             ("均换手", lambda h: f"{h['avg_turn']}%")]))
    else:
        parts.append("#### 🌱 蓄势待发 — 今日 0 只")

    # 🎯 今日突破（命中）
    if hits:
        hits.sort(key=lambda x: x["rise"], reverse=True)
        parts.append(make_table(
            hits[: CONFIG["max_push_each"]],
            f"\n#### 🎯 今日突破（蹲到的地板股启动了）— {len(hits)}只\n",
            [("代码", lambda h: h["code"]), ("名称", lambda h: h["name"]),
             ("现价", lambda h: h["close"]),
             ("突破幅度", lambda h: f"+{h['rise']}%"),
             ("蹲到日", lambda h: h["flagged"])]))

    # ✅ 已兑现档案
    if archive:
        items = sorted(archive.items(), key=lambda kv: -kv[1].get("peak_rise", 0))
        rows = [{"code": c, "name": a.get("name", ""),
                 "peak": a.get("peak_rise", 0), "first": a.get("first_date", "")}
                for c, a in items[: CONFIG["max_archive_push"]]]
        parts.append(make_table(
            rows,
            f"\n#### ✅ 已兑现档案（地板→突破⭐）— 累计{len(archive)}只（今日新进{len(hits)}）\n",
            [("代码", lambda h: h["code"]), ("名称", lambda h: h["name"]),
             ("最高突破", lambda h: f"+{h['peak']}%"), ("首记", lambda h: h["first"])]))
    else:
        parts.append("\n#### ✅ 已兑现档案 — 暂无（等蹲到的地板股突破后自动累积）")

    foot = f"\n> 盯盘地板股 {len(wl)} 只。"
    foot += "\n> 机械筛选，非投资建议。地量横盘后不一定突破，请自行复核。"
    parts.append(foot)

    title = f"🌱地板股 {today}：在蹲{len(floors)}/突破{len(hits)}/已兑现累计{len(archive)}"
    server_chan_push(title, "\n".join(parts))
    save_json(CONFIG["state_file"], {"last_push_date": today})
    print(f"完成。蓄势{len(floors)} 突破{len(hits)} 盯盘{len(wl)} 档案{len(archive)}")


if __name__ == "__main__":
    main()
