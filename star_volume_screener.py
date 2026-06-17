# -*- coding: utf-8 -*-
"""
星星量筛选器 (star_volume_screener) v2
--------------------------------------
形态：底部 + 突然巨量(星星量) + 启动。参照 宗申动力 001696。

本版改动：
  1) 不再用 akshare 的全市场快照（在国外服务器上一次拉 5000 只会超时）。
     改成自己分页、小批量、带浏览器头、多个备用域名去抓东方财富榜单，稳得多。
  2) 推送分两组：
     🚀 即将启动：星星量刚出现、仍在底部、价格还没大涨（重点关注）
     ✅ 已暴涨  ：已从底部冲上一大段（用来验证形态抓得对不对）

数据源：东方财富。推送：Server酱。SendKey 放仓库 Secret：SERVERCHAN_SENDKEY。
说明：本工具按固定技术条件机械筛选，命中 ≠ 买入建议，请自行复核风险。
"""

import os
import time
import datetime as dt

import requests
import pandas as pd
import akshare as ak


# ============================================================
# 配置区
# ============================================================
CONFIG = {
    # ---- 候选池（榜单粗筛）----
    "pool_turnover_min": 5.0,    # 进入候选的最低换手率(%)，按换手率从高到低抓
    "max_candidates": 800,       # 最多确认多少只
    "price_min": 2.0,
    "price_max": 200.0,
    "mktcap_min_yi": 20.0,       # 总市值下限(亿)，设 0 关闭
    "exclude_kechuang_beijing": True,  # 剔除科创板(688)/北交所/B股

    # ---- 形态确认（日K）----
    "hist_days": 150,
    "baseline_window": 60,       # 基准均量窗口
    "surge_window": 3,           # 看最近几日是否出现巨量
    "vol_mult": 2.5,             # 巨量倍数（星星量核心阈值）
    "low_lookback": 60,          # 在最近多少日内创过新低算"底部"
    "low_recent_days": 20,       # 最低点要出现在最近多少日内

    # ---- 两组的分界 ----
    "surged_rise_min": 35.0,     # 已暴涨组：离阶段底涨幅 >= 此值(%)
    "emerging_rise_max": 25.0,   # 即将启动组：离阶段底涨幅 <= 此值(%)
    "surge_fresh_days": 2,       # 即将启动组：巨量须出现在最近几天内
    "rise5_min_surged": 10.0,    # 已暴涨组：近5日涨幅下限(%)

    # ---- 运行控制 ----
    "request_sleep": 0.2,
    "retries": 3,
    "max_push_each": 25,         # 每组最多列多少只
}

SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "").strip()

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
}
# 多个备用域名，挨个试，哪个通用哪个
EM_HOSTS = ["push2.eastmoney.com", "1.push2.eastmoney.com",
            "7.push2.eastmoney.com", "13.push2.eastmoney.com",
            "82.push2.eastmoney.com"]


# ============================================================
# 工具函数
# ============================================================
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


def calc_macd(close, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    bar = (dif - dea) * 2
    return dif, dea, bar


def server_chan_push(title, desp):
    if not SENDKEY:
        print("⚠️ 未设置 SERVERCHAN_SENDKEY，只打印：\n", title, "\n", desp)
        return
    url = f"https://sctapi.ftqq.com/{SENDKEY}.send"
    try:
        r = requests.post(url, data={"title": title, "desp": desp}, timeout=20)
        print("推送结果：", r.status_code, r.text[:200])
    except Exception as e:
        print("推送失败：", e)


def fetch_clist_page(pn, pz=200, fid="f8"):
    """抓东方财富榜单的一页。fid=f8 按换手率排序，po=1 降序。
    返回 list[dict]，失败返回 None。会自动换备用域名。"""
    params = {
        "pn": pn, "pz": pz, "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2", "fid": fid,
        "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23",  # 沪深A股(不含科创/北交所)
        "fields": "f2,f3,f5,f8,f10,f12,f14,f20",
        "_": str(int(time.time() * 1000)),
    }
    for host in EM_HOSTS:
        url = f"https://{host}/api/qt/clist/get"
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=15)
            j = r.json()
            diff = (j or {}).get("data", {}).get("diff")
            if diff:
                return diff
        except Exception:
            continue
    return None


def get_candidate_pool():
    """分页抓榜单（按换手率从高到低），返回候选 DataFrame。"""
    rows = []
    pn = 1
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
            break  # 后面的页拉不到就停
        stop = False
        for d in diff:
            turn = to_num(d.get("f8"))
            if turn is None:
                continue
            if turn < CONFIG["pool_turnover_min"]:
                stop = True
                break
            rows.append({
                "code": str(d.get("f12")),
                "name": d.get("f14"),
                "price": to_num(d.get("f2")),
                "chg": to_num(d.get("f3")),
                "turnover": turn,
                "vr": to_num(d.get("f10")),
                "mktcap": to_num(d.get("f20")),
            })
        if stop:
            break
        pn += 1
        time.sleep(0.2)

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # 过滤
    df = df[df["name"].astype(str).str.contains("ST|退") == False]  # noqa
    df = df[df["price"].notna() & (df["price"] >= CONFIG["price_min"])
            & (df["price"] <= CONFIG["price_max"])]
    if CONFIG["exclude_kechuang_beijing"]:
        df = df[~df["code"].astype(str).str.startswith(("688", "8", "4", "9"))]
    if CONFIG["mktcap_min_yi"] > 0:
        df = df[df["mktcap"].notna() & (df["mktcap"] >= CONFIG["mktcap_min_yi"] * 1e8)]
    return df.reset_index(drop=True)


def confirm_and_classify(code):
    """拉日K，算指标，分类。返回 ('A'/'B', info) 或 None。
    A=已暴涨(验证)  B=即将启动(重点)"""
    end = dt.date.today().strftime("%Y%m%d")
    start = (dt.date.today() - dt.timedelta(days=CONFIG["hist_days"])).strftime("%Y%m%d")
    k = safe_call(ak.stock_zh_a_hist, symbol=code, period="daily",
                  start_date=start, end_date=end, adjust="qfq", timeout=20)
    if k is None or len(k) < CONFIG["baseline_window"] + 5:
        return None

    k = k.rename(columns={"收盘": "close", "成交量": "vol", "最高": "high",
                          "最低": "low", "换手率": "turnover", "涨跌幅": "pct"})
    for c in ["close", "vol", "high", "low", "pct"]:
        if c in k:
            k[c] = pd.to_numeric(k[c], errors="coerce")
    k = k.dropna(subset=["close", "vol"]).reset_index(drop=True)
    if len(k) < CONFIG["baseline_window"] + 5:
        return None

    close, vol = k["close"], k["vol"]
    sw, bw = CONFIG["surge_window"], CONFIG["baseline_window"]

    # 巨量（星星量）：近 sw 日最大量 / 前 bw 日均量
    baseline_vol = vol.iloc[-(bw + sw):-sw].mean()
    if not baseline_vol or baseline_vol <= 0:
        return None
    vol_ratio = vol.iloc[-sw:].max() / baseline_vol
    if vol_ratio < CONFIG["vol_mult"]:
        return None

    # 底部：阶段低点须出现在最近 low_recent_days 内
    lb = CONFIG["low_lookback"]
    seg = close.iloc[-lb:] if len(close) >= lb else close
    low_pos_from_end = len(seg) - 1 - int(seg.values.argmin())
    if low_pos_from_end > CONFIG["low_recent_days"]:
        return None
    stage_low = seg.min()
    rise_from_low = (close.iloc[-1] / stage_low - 1) * 100

    # 均线、MACD、近5日涨幅
    ma5 = close.rolling(5).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    rise5 = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) > 6 else 0
    dif, dea, bar = calc_macd(close)
    macd_up = dif.iloc[-1] > dea.iloc[-1] and bar.iloc[-1] > 0
    macd_turning = bar.iloc[-1] > bar.iloc[-2]  # 红柱在放大/绿柱在收缩

    info = {
        "code": code,
        "close": round(float(close.iloc[-1]), 2),
        "pct": round(float(k["pct"].iloc[-1]), 1) if "pct" in k else None,
        "rise5": round(rise5, 1),
        "rise_from_low": round(rise_from_low, 1),
        "vol_ratio": round(float(vol_ratio), 2),
        "turnover": round(float(k["turnover"].iloc[-1]), 1) if "turnover" in k else None,
    }

    # A 组：已暴涨（离底大、站上MA20、趋势确立）
    if (rise_from_low >= CONFIG["surged_rise_min"] and not pd.isna(ma20)
            and close.iloc[-1] > ma20 and macd_up and rise5 >= CONFIG["rise5_min_surged"]):
        return ("A", info)

    # B 组：即将启动（离底小、巨量新鲜、刚站上MA5、动能转好）
    recent = vol.iloc[-CONFIG["low_recent_days"]:]
    max_vol_pos = len(recent) - 1 - int(recent.values.argmax())
    if (rise_from_low <= CONFIG["emerging_rise_max"]
            and max_vol_pos <= CONFIG["surge_fresh_days"]
            and not pd.isna(ma5) and close.iloc[-1] > ma5
            and (macd_up or macd_turning)):
        return ("B", info)

    return None


# ============================================================
# 主流程
# ============================================================
def make_table(items, head):
    lines = [head,
             "| 代码 | 名称 | 现价 | 今日涨 | 离底涨幅 | 量比(vs60日) | 换手 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for h in items:
        lines.append(f"| {h['code']} | {h['name']} | {h['close']} | "
                     f"{h['pct']}% | {h['rise_from_low']}% | {h['vol_ratio']}x | {h['turnover']}% |")
    return "\n".join(lines)


def main():
    today = dt.date.today().strftime("%Y-%m-%d")
    print(f"=== 星星量筛选 v2 {today} ===")

    try:
        pool = get_candidate_pool()
    except Exception as e:
        server_chan_push(f"❌ 数据源出错 {today}",
                         f"{e}\n可稍后手动重跑一次。\n> 自动筛选器通知")
        return

    print(f"候选池：{len(pool)} 只")
    if pool.empty:
        server_chan_push(f"星星量 {today}：候选为空",
                         "榜单拉到了但过滤后为空，可能阈值偏严。\n> 自动筛选器通知")
        return

    group_a, group_b = [], []
    for _, row in pool.head(CONFIG["max_candidates"]).iterrows():
        code = str(row["code"])
        res = confirm_and_classify(code)
        if res:
            grp, info = res
            info["name"] = row.get("name", "")
            (group_a if grp == "A" else group_b).append(info)
            print(f"  [{grp}] {code} {info['name']} 量比{info['vol_ratio']}x 离底{info['rise_from_low']}%")
        time.sleep(CONFIG["request_sleep"])

    group_b.sort(key=lambda x: x["vol_ratio"], reverse=True)
    group_a.sort(key=lambda x: x["rise_from_low"], reverse=True)
    group_b = group_b[: CONFIG["max_push_each"]]
    group_a = group_a[: CONFIG["max_push_each"]]

    parts = []
    if group_b:
        parts.append(make_table(
            group_b, f"#### 🚀 即将启动（星星量出现、尚未大涨）— {len(group_b)}只\n"))
    else:
        parts.append("#### 🚀 即将启动 — 今日 0 只")
    if group_a:
        parts.append(make_table(
            group_a, f"\n#### ✅ 已暴涨（验证形态用）— {len(group_a)}只\n"))
    else:
        parts.append("\n#### ✅ 已暴涨 — 今日 0 只")
    parts.append("\n> 自动筛选结果，非投资建议，请自行复核风险。"
                 "已暴涨组多为超买，仅供验证逻辑。")

    title = f"⭐星星量 {today}：将启动{len(group_b)} / 已暴涨{len(group_a)}"
    server_chan_push(title, "\n".join(parts))
    print("完成。")


if __name__ == "__main__":
    main()
