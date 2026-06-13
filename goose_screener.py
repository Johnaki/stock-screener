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
            msg = str(e)
            if '频率超限' in msg:
                print(f"频率超限，等待{wait}秒... ({attempt+1}/{retries})")
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

def collect_market_data(pro, last_date, days=65):
    print(f"批量收集近{days}个交易日全市场数据...")
    all_frames = []
    collected  = 0
    current    = datetime.strptime(last_date, '%Y%m%d')

    for i in range(days + 45):
        if collected >= days:
            break
        date_str = (current - timedelta(days=i)).strftime('%Y%m%d')
        data = safe_call(pro.daily, trade_date=date_str, wait=65)
        if data is not None and len(data) > 0:
            all_frames.append(data[['ts_code','trade_date','open','high','low','close','vol']])
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
    """
    鹅张嘴三要素：
    1. 均线长期粘合（底部区域，20天以上MAs压缩）
    2. 今日一阳穿多线（收盘价突破MA5/10/20/30）
    3. 成交量明显放大（≥1.5倍均量）
    """
    print("开始检测鹅张嘴（均线粘合底部突破）...")

    market_df = market_df[market_df['ts_code'].apply(is_valid_code)].copy()
    for col in ['open','high','low','close','vol']:
        market_df[col] = pd.to_numeric(market_df[col], errors='coerce')
    market_df = market_df.dropna(subset=['close','vol'])

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

        # ── 计算均线 ──
        ma5  = close.rolling(5).mean()
        ma10 = close.rolling(10).mean()
        ma20 = close.rolling(20).mean()
        ma30 = close.rolling(30).mean()
        ma60 = close.rolling(60).mean()

        c      = close.iloc[-1]   # 今日收盘
        c_ma5  = ma5.iloc[-1]
        c_ma10 = ma10.iloc[-1]
        c_ma20 = ma20.iloc[-1]
        c_ma30 = ma30.iloc[-1]
        c_ma60 = ma60.iloc[-1]

        if any(pd.isna([c_ma5, c_ma10, c_ma20, c_ma30, c_ma60])):
            continue

        # ══ 条件1：今日一阳穿多线 ══
        # 收盘价需同时突破MA5/MA10/MA20/MA30
        if not (c > c_ma5 and c > c_ma10 and c > c_ma20 and c > c_ma30):
            continue

        # 今日涨幅 > 2%（有力度）
        prev_close = close.iloc[-2] if len(close) > 1 else c
        pct_chg = (c - prev_close) / prev_close * 100
        if pct_chg < 2.0:
            continue

        # ══ 条件2：前期均线长期粘合（过去20天内MAs曾高度压缩）══
        # 检查过去5~25天内，是否存在至少15天均线粘合（极差/均值<4%）
        compression_days = 0
        for k in range(5, min(26, len(g))):
            idx_k = -(k+1)
            try:
                mk5  = ma5.iloc[idx_k]
                mk10 = ma10.iloc[idx_k]
                mk20 = ma20.iloc[idx_k]
                mk30 = ma30.iloc[idx_k]
                mk60 = ma60.iloc[idx_k]
                if any(pd.isna([mk5, mk10, mk20, mk30, mk60])):
                    continue
                mk_vals   = [mk5, mk10, mk20, mk30, mk60]
                mk_range  = max(mk_vals) - min(mk_vals)
                mk_mean   = np.mean(mk_vals)
                if mk_mean > 0 and mk_range / mk_mean < 0.04:
                    compression_days += 1
            except:
                continue

        if compression_days < 10:   # 至少10天粘合才算"长期压缩"
            continue

        # ══ 条件3：底部区域（股价在近1年低位）══
        year_low  = close.iloc[-min(250, len(close)):].min()
        year_high = close.iloc[-min(250, len(close)):].max()
        price_pos = (c - year_low) / (year_high - year_low) if year_high > year_low else 1.0
        if price_pos > 0.45:        # 只要价格在年内低位45%以内
            continue

        # ══ 条件4：成交量放大（今日量 > 近20日均量1.5倍）══
        vol_today = vol.iloc[-1]
        vol_ma20  = vol.iloc[-21:-1].mean()
        vol_ratio = vol_today / vol_ma20 if vol_ma20 > 0 else 0
        if vol_ratio < 1.5:
            continue

        # ── 粘合度（取突破前最紧的一天）──
        best_compression = 999
        for k in range(5, min(26, len(g))):
            idx_k = -(k+1)
            try:
                mk_vals  = [ma5.iloc[idx_k], ma10.iloc[idx_k],
                            ma20.iloc[idx_k], ma30.iloc[idx_k], ma60.iloc[idx_k]]
                if any(pd.isna(mk_vals)):
                    continue
                comp = (max(mk_vals) - min(mk_vals)) / np.mean(mk_vals)
                best_compression = min(best_compression, comp)
            except:
                continue

        # ── 综合评分 ──
        comp_score  = max(0, (1 - best_compression / 0.04)) * 35
        vol_score   = min((vol_ratio - 1.5) * 10, 25)
        pct_score   = min(pct_chg * 3, 20)
        pos_score   = (1 - price_pos) * 20   # 越低位分越高
        total_score = round(comp_score + vol_score + pct_score + pos_score, 1)

        results.append({
            'code':          ts_code,
            'price':         round(float(c), 2),
            'pct_chg':       round(pct_chg, 2),
            'compression':   round(best_compression * 100, 2),
            'compress_days': compression_days,
            'vol_ratio':     round(vol_ratio, 2),
            'price_pos':     round(price_pos * 100, 1),
            'score':         total_score,
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
            f"数据日期：{date_str}\n\n今日未发现均线粘合底部突破信号。"
        )

    lines = [f"## 🦢 千金难买鹅张嘴 {today}（数据：{date_str}）\n"]
    lines.append(f"> 共发现 **{len(results)}** 支 | 均线长期粘合后今日一阳穿多线，底部启动！\n")
    lines.append("\n### TOP 15 鹅张嘴信号\n")
    lines.append("| 代码 | 现价 | 今日涨幅 | 粘合度 | 粘合天数 | 量比 | 价格分位 | 评分 |")
    lines.append("|------|------|----------|--------|----------|------|----------|------|")

    for r in results[:15]:
        lines.append(
            f"| {r['code']} | {r['price']} | +{r['pct_chg']}% | "
            f"{r['compression']}% | {r['compress_days']}天 | "
            f"{r['vol_ratio']}x | {r['price_pos']}% | {r['score']} |"
        )

    lines.append("\n---")
    lines.append("**粘合度**：越小均线越紧 | **粘合天数**：压缩持续时长 | **价格分位**：越低越在底部")
    lines.append("\n⚠️ 量化技术筛选，不构成投资建议，请结合基本面自行判断。")

    return f"🦢 鹅张嘴预警 {today} | 发现{len(results)}支底部启动", "\n".join(lines)

def main():
    print(f"===== 🦢 鹅张嘴扫描开始 {datetime.now()} =====")
    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()

    last_date = get_last_trade_date(pro)
    time.sleep(2)

    market_df = collect_market_data(pro, last_date, days=65)
    if market_df is None:
        send_wechat("🦢 鹅张嘴扫描失败", "数据获取失败")
        return

    results        = detect_goose_patterns(market_df)
    title, content = format_message(results, last_date)
    send_wechat(title, content)
    print("===== 完成 =====")

if __name__ == "__main__":
    main()
