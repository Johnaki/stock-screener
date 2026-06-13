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
    if code.startswith(('688', '689', '83', '87', '43', '40', '92', '93')):
        return False
    return ts_code.endswith('.SZ') or ts_code.endswith('.SH')

def safe_call(func, retries=3, wait=65, **kwargs):
    for attempt in range(retries):
        try:
            result = func(**kwargs)
            return result
        except Exception as e:
            msg = str(e)
            if '频率超限' in msg:
                print(f"频率超限，等待{wait}秒... ({attempt+1}/{retries})")
                time.sleep(wait)
            else:
                print(f"错误: {e}")
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

def collect_market_data(pro, last_date, days=65):
    """批量获取近65个交易日全市场行情，一次拉一天所有股票"""
    print(f"批量收集近{days}个交易日数据...")
    all_frames = []
    collected = 0
    current = datetime.strptime(last_date, '%Y%m%d')

    for i in range(days + 45):
        if collected >= days:
            break
        date_str = (current - timedelta(days=i)).strftime('%Y%m%d')
        data = safe_call(pro.daily, trade_date=date_str, wait=65)
        if data is not None and len(data) > 0:
            all_frames.append(data[['ts_code', 'trade_date', 'close', 'vol']])
            collected += 1
            if collected % 10 == 0:
                print(f"  已收集 {collected}/{days} 天")
        time.sleep(0.4)

    if not all_frames:
        return None

    df = pd.concat(all_frames, ignore_index=True)
    print(f"数据收集完成：{collected}个交易日，{len(df)}条记录")
    return df

def detect_goose_patterns(market_df):
    """对全市场计算均线，批量检测鹅张嘴形态"""
    print("开始计算均线并检测鹅张嘴...")

    market_df = market_df[market_df['ts_code'].apply(is_valid_code)].copy()
    market_df['close'] = pd.to_numeric(market_df['close'], errors='coerce')
    market_df['vol']   = pd.to_numeric(market_df['vol'],   errors='coerce')
    market_df = market_df.dropna(subset=['close', 'vol'])

    results = []
    grouped = market_df.groupby('ts_code')
    total   = len(grouped)

    for idx, (ts_code, group) in enumerate(grouped):
        if idx % 500 == 0 and idx > 0:
            print(f"  进度: {idx}/{total}，已发现 {len(results)} 支")

        g = group.sort_values('trade_date').reset_index(drop=True)
        if len(g) < 62:
            continue

        close = g['close']
        vol   = g['vol']

        ma5  = close.rolling(5).mean()
        ma10 = close.rolling(10).mean()
        ma20 = close.rolling(20).mean()
        ma30 = close.rolling(30).mean()
        ma60 = close.rolling(60).mean()

        c_ma5  = ma5.iloc[-1]
        c_ma10 = ma10.iloc[-1]
        c_ma20 = ma20.iloc[-1]
        c_ma30 = ma30.iloc[-1]
        c_ma60 = ma60.iloc[-1]
        c_close = close.iloc[-1]

        if any(pd.isna([c_ma5, c_ma10, c_ma20, c_ma30, c_ma60])):
            continue

        # ── 条件1：均线粘合（5线极差/均值 < 5%）──
        ma_vals     = [c_ma5, c_ma10, c_ma20, c_ma30, c_ma60]
        ma_range    = max(ma_vals) - min(ma_vals)
        ma_mean     = np.mean(ma_vals)
        compression = ma_range / ma_mean if ma_mean > 0 else 1.0
        if compression > 0.05:
            continue

        # ── 条件2：MA5率先上翘（比3天前高）──
        if len(ma5) < 5 or pd.isna(ma5.iloc[-4]):
            continue
        ma5_3ago   = ma5.iloc[-4]
        ma5_rising = c_ma5 > ma5_3ago
        if not ma5_rising:
            continue

        # ── 条件3：MA5是5线中最高的（已率先脱离粘合）──
        if c_ma5 < max(c_ma10, c_ma20, c_ma30, c_ma60):
            continue

        # ── 条件4：成交量温和放大（1.1x ~ 5x）──
        vol_5  = vol.iloc[-5:].mean()
        vol_20 = vol.iloc[-21:-1].mean()
        vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 0
        if not (1.1 < vol_ratio < 5.0):
            continue

        # ── 条件5：股价在均线附近（不追高）──
        if abs(c_close - c_ma20) / c_ma20 > 0.08:
            continue

        # ── 综合评分 ──
        compression_score = (1 - compression / 0.05) * 40
        vol_score         = min((vol_ratio - 1) * 20, 30)
        ma5_angle         = (c_ma5 - ma5_3ago) / ma5_3ago * 100
        angle_score       = min(ma5_angle * 15, 30)
        total_score       = round(compression_score + vol_score + angle_score, 1)

        price_pct = round((close.iloc[-60:] < c_close).sum() / min(len(close), 60) * 100, 1)

        results.append({
            'code':        ts_code,
            'price':       round(float(c_close), 2),
            'compression': round(compression * 100, 2),
            'vol_ratio':   round(vol_ratio, 2),
            'ma5_angle':   round(ma5_angle, 3),
            'price_pct':   price_pct,
            'score':       total_score,
        })

    results.sort(key=lambda x: x['score'], reverse=True)
    print(f"检测完成，共发现 {len(results)} 支鹅张嘴")
    return results

def format_message(results, last_date):
    date_str = f"{last_date[:4]}-{last_date[4:6]}-{last_date[6:]}"
    today    = datetime.now().strftime('%Y-%m-%d')

    if not results:
        return (
            f"🦢 鹅张嘴扫描 {today} | 暂无信号",
            f"数据日期：{date_str}\n\n今日未发现均线粘合启动信号。"
        )

    lines = [f"## 🦢 千金难买鹅张嘴 {today}（数据：{date_str}）\n"]
    lines.append(f"> 共发现 **{len(results)}** 支 | 均线粘合后MA5率先上翘，量能温和配合\n")
    lines.append("\n### TOP 15 鹅张嘴信号\n")
    lines.append("| 代码 | 现价 | 粘合度 | 量比 | 价格分位 | 评分 |")
    lines.append("|------|------|--------|------|----------|------|")

    for r in results[:15]:
        lines.append(
            f"| {r['code']} | {r['price']} | "
            f"{r['compression']}% | {r['vol_ratio']}x | "
            f"{r['price_pct']}% | {r['score']} |"
        )

    lines.append("\n---")
    lines.append("**粘合度**：越小均线越紧 | **量比**：近5日量/近20日均量")
    lines.append("\n⚠️ 量化技术筛选，不构成投资建议，请结合基本面自行判断。")

    return f"🦢 鹅张嘴预警 {today} | 发现{len(results)}支", "\n".join(lines)

def main():
    print(f"===== 🦢 鹅张嘴扫描开始 {datetime.now()} =====")
    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()

    last_date = get_last_trade_date(pro)
    time.sleep(2)

    market_df = collect_market_data(pro, last_date, days=65)
    if market_df is None:
        send_wechat("🦢 鹅张嘴扫描失败", "数据获取失败，请检查Token")
        return

    results   = detect_goose_patterns(market_df)
    title, content = format_message(results, last_date)
    send_wechat(title, content)
    print("===== 完成 =====")

if __name__ == "__main__":
    main()
