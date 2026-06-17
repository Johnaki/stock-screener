# -*- coding: utf-8 -*-
"""
星星量筛选器 (star_volume_screener)
-----------------------------------
形态：底部 + 突然巨量 + 拉升启动（参照 宗申动力 001696 的图）

数据源：akshare（东方财富）。推送：Server酱 Turbo。
SendKey 放在 GitHub 仓库的 Secret 里，名字叫 SERVERCHAN_SENDKEY。

注意：这是按固定技术条件自动筛选的工具，命中 ≠ 买入建议。
我不是投资顾问，结果请自己复核风险后再决定。
"""

import os
import time
import datetime as dt

import requests
import pandas as pd
import akshare as ak


# ============================================================
# 配置区（都可以自己改）
# ============================================================
CONFIG = {
    # ---- 第一轮：实时快照粗筛 ----
    "today_change_min": 3.0,     # 今日涨幅下限 (%)
    "turnover_min": 8.0,         # 今日换手率下限 (%)
    "volume_ratio_min": 1.2,     # 今日量比下限
    "price_min": 2.0,            # 股价下限
    "price_max": 200.0,          # 股价上限
    "mktcap_min_yi": 20.0,       # 总市值下限（亿），设 0 关闭
    "exclude_kechuang_beijing": True,  # 剔除科创板(688)/北交所/B股

    # ---- 第二轮：日K形态确认 ----
    "hist_days": 150,            # 取最近多少自然日的日K
    "baseline_window": 60,       # 基准均量窗口
    "surge_window": 3,           # 看最近几日有没有巨量
    "vol_mult": 2.5,             # 巨量倍数：近几日最大量 / 基准均量
    "rise_window": 5,            # 近多少日累计涨幅
    "rise_pct_min": 12.0,        # 近几日累计涨幅下限 (%)
    "low_lookback": 60,          # 在最近多少日内创过新低算"底部"
    "low_recent_days": 20,       # 最低点要出现在最近多少日内
    "max_rise_from_low": 60.0,   # 距阶段低点最大涨幅 (%)；想抓已涨完的就调大

    # ---- 运行控制 ----
    "request_sleep": 0.2,        # 每只票之间的间隔，防限流
    "max_candidates": 600,       # 第二轮最多确认多少只
    "max_push": 40,              # 推送里最多列多少只
    "retries": 3,                # 单次接口失败重试次数
}

SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "").strip()


# ============================================================
# 工具函数
# ============================================================
def safe_call(func, *args, retries=None, base_sleep=1.0, **kwargs):
    """带重试的接口调用。失败返回 None，不抛异常。"""
    if retries is None:
        retries = CONFIG["retries"]
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt == retries - 1:
                print(f"  接口失败({func.__name__}): {e}")
                return None
            time.sleep(base_sleep * (attempt + 1))
    return None


def calc_macd(close: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    bar = (dif - dea) * 2
    return dif, dea, bar


def server_chan_push(title: str, desp: str) -> None:
    """Server酱推送。免费版每天5条，所以合并成一条发。"""
    if not SENDKEY:
        print("⚠️ 未设置 SERVERCHAN_SENDKEY，只打印不推送：\n")
        print(title, "\n", desp)
        return
    url = f"https://sctapi.ftqq.com/{SENDKEY}.send"
    try:
        r = requests.post(url, data={"title": title, "desp": desp}, timeout=20)
        print("推送结果：", r.status_code, r.text[:200])
    except Exception as e:
        print("推送失败：", e)


def connectivity_check() -> bool:
    """数据源连通性自检：拉一只票的历史数据看看通不通。"""
    print("连通性自检中...")
    end = dt.date.today().strftime("%Y%m%d")
    start = (dt.date.today() - dt.timedelta(days=30)).strftime("%Y%m%d")
    test = safe_call(ak.stock_zh_a_hist, symbol="001696", period="daily",
                     start_date=start, end_date=end, adjust="qfq", timeout=20)
    ok = test is not None and len(test) > 0
    print("自检结果：", "通 ✅" if ok else "不通 ❌")
    return ok


def get_candidate_pool() -> pd.DataFrame:
    """第一轮：实时快照粗筛。"""
    spot = safe_call(ak.stock_zh_a_spot_em)
    if spot is None or len(spot) == 0:
        raise RuntimeError("实时行情接口拉不到数据")

    cols = spot.columns
    def col(*names):
        for n in names:
            if n in cols:
                return n
        return None

    c_code, c_name, c_price = col("代码"), col("名称"), col("最新价")
    c_chg, c_turn, c_vr, c_mktcap = col("涨跌幅"), col("换手率"), col("量比"), col("总市值")

    df = spot.copy()
    for c in [c_price, c_chg, c_turn, c_vr, c_mktcap]:
        if c:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df[~df[c_name].astype(str).str.contains("ST|退", na=False)]
    df = df[df[c_price].notna() & (df[c_price] > 0)]
    df = df[(df[c_price] >= CONFIG["price_min"]) & (df[c_price] <= CONFIG["price_max"])]

    if CONFIG["exclude_kechuang_beijing"]:
        df = df[~df[c_code].astype(str).str.startswith(("688", "8", "4", "9"))]

    if c_chg:
        df = df[df[c_chg] >= CONFIG["today_change_min"]]
    if c_turn:
        df = df[df[c_turn] >= CONFIG["turnover_min"]]
    if c_vr:
        df = df[df[c_vr] >= CONFIG["volume_ratio_min"]]
    if c_mktcap and CONFIG["mktcap_min_yi"] > 0:
        df = df[df[c_mktcap] >= CONFIG["mktcap_min_yi"] * 1e8]

    df = df.rename(columns={c_code: "code", c_name: "name", c_price: "price",
                            c_turn: "turnover", c_chg: "chg", c_vr: "vr"})
    keep = [x for x in ["code", "name", "price", "chg", "turnover", "vr"] if x in df.columns]
    return df[keep].reset_index(drop=True)


def confirm_pattern(code: str):
    """第二轮：拉日K确认形态。命中返回 dict，否则 None。"""
    end = dt.date.today().strftime("%Y%m%d")
    start = (dt.date.today() - dt.timedelta(days=CONFIG["hist_days"])).strftime("%Y%m%d")
    k = safe_call(ak.stock_zh_a_hist, symbol=code, period="daily",
                  start_date=start, end_date=end, adjust="qfq", timeout=20)
    if k is None or len(k) < CONFIG["baseline_window"] + 5:
        return None

    k = k.rename(columns={"收盘": "close", "成交量": "vol", "最高": "high",
                          "最低": "low", "换手率": "turnover", "日期": "date"})
    for c in ["close", "vol", "high", "low"]:
        k[c] = pd.to_numeric(k[c], errors="coerce")
    k = k.dropna(subset=["close", "vol"]).reset_index(drop=True)
    if len(k) < CONFIG["baseline_window"] + 5:
        return None

    close, vol = k["close"], k["vol"]

    sw, bw = CONFIG["surge_window"], CONFIG["baseline_window"]
    recent_max_vol = vol.iloc[-sw:].max()
    baseline_vol = vol.iloc[-(bw + sw):-sw].mean()
    if baseline_vol <= 0:
        return None
    vol_ratio = recent_max_vol / baseline_vol
    if vol_ratio < CONFIG["vol_mult"]:
        return None

    rw = CONFIG["rise_window"]
    if len(close) <= rw:
        return None
    rise = (close.iloc[-1] / close.iloc[-rw - 1] - 1) * 100
    if rise < CONFIG["rise_pct_min"]:
        return None

    ma20 = close.rolling(20).mean().iloc[-1]
    if pd.isna(ma20) or close.iloc[-1] < ma20:
        return None

    lb = CONFIG["low_lookback"]
    seg = close.iloc[-lb:] if len(close) >= lb else close
    low_pos_from_end = len(seg) - 1 - int(seg.values.argmin())
    if low_pos_from_end > CONFIG["low_recent_days"]:
        return None
    stage_low = seg.min()
    rise_from_low = (close.iloc[-1] / stage_low - 1) * 100
    if rise_from_low > CONFIG["max_rise_from_low"]:
        return None

    dif, dea, bar = calc_macd(close)
    if not (dif.iloc[-1] > dea.iloc[-1] and bar.iloc[-1] > 0):
        return None

    return {
        "code": code,
        "vol_ratio": round(vol_ratio, 2),
        "rise": round(rise, 1),
        "rise_from_low": round(rise_from_low, 1),
        "turnover": round(float(k["turnover"].iloc[-1]), 1) if "turnover" in k else None,
        "close": round(float(close.iloc[-1]), 2),
    }


# ============================================================
# 主流程
# ============================================================
def main():
    today = dt.date.today().strftime("%Y-%m-%d")
    print(f"=== 星星量筛选 {today} ===")

    if not connectivity_check():
        server_chan_push(
            f"❌ 数据源连不上 {today}",
            "GitHub 服务器访问东方财富接口失败（可能被临时限流或网络问题）。"
            "可以稍后手动重跑一次试试。\n> 自动筛选器通知")
        return

    try:
        pool = get_candidate_pool()
    except Exception as e:
        server_chan_push(f"❌ 筛选器出错 {today}", f"获取行情失败：{e}\n> 自动筛选器通知")
        return

    print(f"第一轮粗筛候选：{len(pool)} 只")
    pool = pool.head(CONFIG["max_candidates"])

    hits = []
    for _, row in pool.iterrows():
        code = str(row["code"])
        res = confirm_pattern(code)
        if res:
            res["name"] = row.get("name", "")
            hits.append(res)
            print(f"  ✅ {code} {res['name']}  量比{res['vol_ratio']}x 涨{res['rise']}%")
        time.sleep(CONFIG["request_sleep"])

    hits.sort(key=lambda x: x["vol_ratio"], reverse=True)
    hits = hits[: CONFIG["max_push"]]

    if hits:
        lines = [
            f"#### 星星量·底部巨量启动  共 {len(hits)} 只\n",
            "| 代码 | 名称 | 现价 | 近5日涨 | 离底涨幅 | 量比(vs60日) | 换手 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for h in hits:
            lines.append(
                f"| {h['code']} | {h['name']} | {h['close']} | "
                f"{h['rise']}% | {h['rise_from_low']}% | {h['vol_ratio']}x | {h['turnover']}% |")
        lines.append("\n> 自动筛选结果，非投资建议，请自行复核风险。")
        desp = "\n".join(lines)
        title = f"⭐ 星星量 {today}：{len(hits)}只"
    else:
        desp = "今天没有符合「底部巨量启动」条件的票。\n> 自动筛选结果，非投资建议。"
        title = f"星星量 {today}：0只"

    server_chan_push(title, desp)
    print("完成。")


if __name__ == "__main__":
    main()
