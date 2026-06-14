import tushare as ts
import pandas as pd
import numpy as np
import requests
import os
import time
from datetime import datetime, timedelta

TUSHARE_TOKEN = os.environ.get('TUSHARE_TOKEN', '')
SEVER_CHAN_KEY = os.environ.get('SEVER_CHAN_KEY', '')

def send_wechat(title, content):
    url = f"https://sctapi.ftqq.com/{SEVER_CHAN_KEY}.send"
    data = {"title": title, "desp": content}
    try:
        resp = requests.post(url, data=data, timeout=10)
        print(f"推送结果: {resp.status_code}")
    except Exception as e:
        print(f"推送失败: {e}")

def is_valid_code(ts_code):
    code = ts_code.split('.')[0]
    if code.startswith(('688','689','83','87','43','40','92','93')):
        return False
    return ts_code.endswith('.SZ') or ts_code.endswith('.SH')

def safe_call(func, retries=3, wait=65, **kwargs):
    for attempt in range(retries):
        try:
            return func(**kwargs)
        except Exception as e:
            if '频率超限' in str(e):
                print(f"频率超限，等待{wait}秒...({attempt+1}/{retries})")
                time.sleep(wait)
            else:
                time.sleep(5)
    return None

def get_last_trade_date(pro):
    for i in range(7):
        date = (datetime.now() - timedelta(days=i)).strftime('%Y%m%d')
        try:
            test = pro.daily(ts_code='000001.SZ', start_date=date, end_date=date)
            if test is not None and len(test) > 0:
                print(f"最近交易日: {date}")
                return date
        except:
            pass
        time.sleep(2)
    return (datetime.now() - timedelta(days=3)).strftime('%Y%m%d')

def collect_market_data(pro, last_date, days=150):
    """
    批量拉取近150个交易日全市场数据
    150天 = 120天回看窗口 + 30天MA55计算缓冲
    """
    print(f"批量收集近{days}个交易日数据（含high字段）...")
    all_frames = []
    collected  = 0
    current    = datetime.strptime(last_date, '%Y%m%d')

    for i in range(days + 60):
        if collected >= days:
            break
        date_str = (current - timedelta(days=i)).strftime('%Y%m%d')
        data = safe_call(pro.daily, trade_date=date_str, wait=65)
        if data is not None and len(data) > 0:
            all_frames.append(
                data[['ts_code','trade_date','close','high','vol']]
            )
            collected += 1
            if collected % 25 == 0:
                print(f"  已收集 {collected}/{days} 天")
        time.sleep(0.4)

    if not all_frames:
        return None

    df = pd.concat(all_frames, ignore_index=True)
    print(f"收集完成：{collected}个交易日，{len(df)}条记录")
    return df

def detect_goose(g):
    """
    鹅张嘴检测（MA13 / MA35 / MA55）

    六步逻辑：
    ① 计算MA13/MA35/MA55
    ② 找鹅头（回看120日内最高high）
    ③ 找鹅脖子（鹅头之前MA13>MA35>MA55的连续天数 ≥10天）
    ④ 找压缩期（鹅头之后三线靠拢 ≥5天）
    ⑤ 当前张嘴（MA13上翘且领先MA35）
    ⑥ 判断信号（🔴突破鹅头 or 🟡张嘴预警）
    """
    g = g.reset_index(drop=True)
    close = pd.to_numeric(g['close'], errors='coerce')
    high  = pd.to_numeric(g['high'],  errors='coerce')
    vol   = pd.to_numeric(g['vol'],   errors='coerce')

    if len(g) < 60 or close.isna().sum() > 5:
        return None

    ma13 = close.rolling(13).mean()
    ma35 = close.rolling(35).mean()
    ma55 = close.rolling(55).mean()

    if pd.isna(ma55.iloc[-1]):
        return None

    n = len(close)

    # ── ① 鹅头：回看120日内最高price ──
    lb = min(120, n)
    win_high = high.iloc[n - lb:]
    goose_head  = win_high.max()
    head_rel    = (n - lb) + int(np.argmax(win_high.values))

    # 鹅头必须距今至少6天（留出压缩时间）
    if head_rel > n - 6:
        return None

    # ── ② 鹅脖子：鹅头之前MA13>MA35>MA55连续天数 ──
    neck_days = 0
    for i in range(head_rel - 1, max(head_rel - 70, 54), -1):
        v13, v35, v55 = ma13.iloc[i], ma35.iloc[i], ma55.iloc[i]
        if any(pd.isna([v13, v35, v55])):
            break
        if v13 > v35 > v55:
            neck_days += 1
        else:
            break

    if neck_days < 10:
        return None

    # ── ③ 压缩期：鹅头之后三线靠拢（极差/均值<5%）≥5天 ──
    compress_days = 0
    for i in range(head_rel + 1, n - 1):
        v13, v35, v55 = ma13.iloc[i], ma35.iloc[i], ma55.iloc[i]
        if any(pd.isna([v13, v35, v55])):
            continue
        vals   = [v13, v35, v55]
        spread = (max(vals) - min(vals)) / np.mean(vals)
        if spread < 0.05:
            compress_days += 1

    if compress_days < 5:
        return None

    # ── ④ 当前张嘴：MA13上翘且接近/超过MA35 ──
    c13, c35, c55 = ma13.iloc[-1], ma35.iloc[-1], ma55.iloc[-1]
    if any(pd.isna([c13, c35, c55])):
        return None

    if len(ma13) < 7 or pd.isna(ma13.iloc[-6]):
        return None
    ma13_5ago   = ma13.iloc[-6]
    ma13_rising = c13 > ma13_5ago

    if not ma13_rising:
        return None

    # MA13需达到MA35的97%以上（已经非常接近或超过）
    if c13 < c35 * 0.97:
        return None

    # ── ⑤ 成交量 ──
    if len(vol) < 22:
        return None
    vol_5   = vol.iloc[-5:].mean()
    vol_20  = vol.iloc[-21:-1].mean()
    vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 0

    if vol_ratio < 1.3:
        return None

    # ── ⑥ 信号类型 ──
    today_close  = close.iloc[-1]
    distance_pct = (goose_head - today_close) / goose_head * 100

    if today_close >= goose_head and vol_ratio >= 1.5:
        signal = 'breakthrough'   # 🔴 突破验证
    elif today_close < goose_head:
        signal = 'warning'        # 🟡 张嘴预警
    else:
        return None

    # ── 综合评分 ──
    neck_score     = min(neck_days / 30, 1.0) * 30
    vol_score      = min((vol_ratio - 1.0) * 25, 30)
    ma13_angle     = (c13 - ma13_5ago) / ma13_5ago * 100
    angle_score    = min(ma13_angle * 10, 20)
    dist_score     = max(20 - max(distance_pct, 0), 0) if signal == 'warning' else 20
    total_score    = round(neck_score + vol_score + angle_score + dist_score, 1)

    price_pct = round(
        (close.iloc[-min(60, n):] < today_close).sum() / min(60, n) * 100, 1
    )

    return {
        'signal':         signal,
        'score':          total_score,
        'neck_days':      neck_days,
        'compress_days':  compress_days,
        'goose_head':     round(float(goose_head), 2),
        'close':          round(float(today_close), 2),
        'distance_pct':   round(distance_pct, 1),
        'vol_ratio':      round(vol_ratio, 2),
        'price_pct':      price_pct,
    }

def screen_all(market_df):
    print("开始全市场鹅张嘴检测...")
    market_df = market_df[market_df['ts_code'].apply(is_valid_code)].copy()
    for col in ['close', 'high', 'vol']:
        market_df[col] = pd.to_numeric(market_df[col], errors='coerce')
    market_df.dropna(subset=['close', 'high', 'vol'], inplace=True)

    breakthrough, warning = [], []
    grouped = market_df.groupby('ts_code')
    total   = len(grouped)

    for idx, (ts_code, group) in enumerate(grouped):
        if idx % 500 == 0 and idx > 0:
            print(f"  进度 {idx}/{total} | 🔴{len(breakthrough)} 🟡{len(warning)}")

        g = group.sort_values('trade_date')
        r = detect_goose(g)
        if r:
            r['code'] = ts_code
            if r['signal'] == 'breakthrough':
                breakthrough.append(r)
                print(f"  🔴 {ts_code} | 突破鹅头{r['goose_head']} | 脖子{r['neck_days']}天 | 评分{r['score']}")
            else:
                warning.append(r)
                print(f"  🟡 {ts_code} | 距鹅头{r['distance_pct']}% | 脖子{r['neck_days']}天 | 评分{r['score']}")

    breakthrough.sort(key=lambda x: x['score'], reverse=True)
    warning.sort(key=lambda x: x['score'], reverse=True)
    print(f"检测完成：🔴{len(breakthrough)} 🟡{len(warning)}")
    return breakthrough, warning

def format_message(breakthrough, warning, last_date):
    date_str = f"{last_date[:4]}-{last_date[4:6]}-{last_date[6:]}"
    today    = datetime.now().strftime('%Y-%m-%d')

    if not breakthrough and not warning:
        return (
            f"🦢 鹅张嘴 {today} | 暂无信号",
            f"数据日期：{date_str}\n\n今日未发现鹅张嘴信号（MA13/MA35/MA55）。"
        )

    lines = [f"## 🦢 千金难买鹅张嘴 {today}（数据：{date_str}）\n"]
    lines.append(f"> 均线：MA13/MA35/MA55 | 鹅头：形态最高价 | 验证：突破鹅头顶\n")
    lines.append(f"> 🔴突破验证 {len(breakthrough)}支 | 🟡张嘴预警 {len(warning)}支\n")

    if breakthrough:
        lines.append("\n### 🔴 突破验证（今日突破鹅头顶 + 放量确认）\n")
        lines.append("| 代码 | 现价 | 鹅头价 | 脖子天数 | 压缩天数 | 量比 | 评分 |")
        lines.append("|------|------|--------|----------|----------|------|------|")
        for r in breakthrough[:10]:
            lines.append(
                f"| {r['code']} | {r['close']} | {r['goose_head']} | "
                f"{r['neck_days']}天 | {r['compress_days']}天 | "
                f"{r['vol_ratio']}x | {r['score']} |"
            )

    if warning:
        lines.append("\n### 🟡 张嘴预警（MA13上翘，等待突破鹅头）\n")
        lines.append("| 代码 | 现价 | 鹅头价 | 距鹅头 | 脖子天数 | 量比 | 评分 |")
        lines.append("|------|------|--------|--------|----------|------|------|")
        for r in warning[:15]:
            lines.append(
                f"| {r['code']} | {r['close']} | {r['goose_head']} | "
                f"-{r['distance_pct']}% | {r['neck_days']}天 | "
                f"{r['vol_ratio']}x | {r['score']} |"
            )

    lines.append("\n---")
    lines.append("🔴买入参考：突破鹅头顶且放量 | 🟡提前关注：MA13上翘蓄势中")
    lines.append("\n⚠️ 量化技术筛选，不构成投资建议，请结合基本面自行判断。")

    title = f"🦢 鹅张嘴 {today} | 🔴{len(breakthrough)}突破 🟡{len(warning)}预警"
    return title, "\n".join(lines)

def main():
    print(f"===== 🦢 鹅张嘴扫描开始 {datetime.now()} =====")
    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()

    last_date = get_last_trade_date(pro)
    time.sleep(2)

    market_df = collect_market_data(pro, last_date, days=150)
    if market_df is None:
        send_wechat("🦢 鹅张嘴扫描失败", "数据获取失败")
        return

    breakthrough, warning = screen_all(market_df)
    title, content = format_message(breakthrough, warning, last_date)
    send_wechat(title, content)
    print("===== 完成 =====")

if __name__ == "__main__":
    main()
