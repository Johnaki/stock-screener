import tushare as ts
import pandas as pd
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

def get_last_trade_date(pro):
    cal = pro.trade_cal(
        exchange='SSE',
        start_date=(datetime.now() - timedelta(days=10)).strftime('%Y%m%d'),
        end_date=datetime.now().strftime('%Y%m%d'),
        is_open='1'
    )
    if cal is not None and len(cal) > 0:
        return cal['cal_date'].iloc[-1]
    return datetime.now().strftime('%Y%m%d')

def screen_stocks(pro):
    print("获取股票列表...")
    stocks = pro.stock_basic(
        exchange='', list_status='L',
        fields='ts_code,name,list_date'
    )
    if stocks is None or len(stocks) == 0:
        print("获取股票列表失败")
        return []

    # 过滤：排除ST、北交所、科创板、上市不足1年
    stocks = stocks[~stocks['name'].str.contains('ST|退', na=False)]
    stocks = stocks[~stocks['ts_code'].str.startswith(('688', '689', '8', '4', '9'))]
    one_year_ago = (datetime.now() - timedelta(days=365)).strftime('%Y%m%d')
    stocks = stocks[stocks['list_date'] <= one_year_ago].reset_index(drop=True)
    print(f"有效股票: {len(stocks)} 支")

    last_date = get_last_trade_date(pro)
    print(f"最近交易日: {last_date}")

    # 一次性获取全市场当日行情
    print("获取全市场行情（单次请求）...")
    daily_all = pro.daily(trade_date=last_date)
    if daily_all is None or len(daily_all) == 0:
        print("获取行情失败")
        return []

    daily_all = daily_all.merge(stocks[['ts_code', 'name']], on='ts_code', how='inner')

    # 初筛：换手率极低（前300名最低换手率）
    if 'turnover_rate' in daily_all.columns:
        daily_all['turnover_rate'] = pd.to_numeric(daily_all['turnover_rate'], errors='coerce').fillna(999)
        candidates_df = daily_all[daily_all['turnover_rate'] > 0].nsmallest(300, 'turnover_rate')
    else:
        candidates_df = daily_all.nsmallest(300, 'vol')

    candidates = candidates_df['ts_code'].tolist()
    print(f"初筛候选: {len(candidates)} 支，开始深度分析...")

    results = []
    start_date = (datetime.now() - timedelta(days=450)).strftime('%Y%m%d')

    for i, ts_code in enumerate(candidates):
        try:
            hist = pro.daily(ts_code=ts_code, start_date=start_date, end_date=last_date)
            time.sleep(0.4)

            if hist is None or len(hist) < 130:
                continue

            hist = hist.sort_values('trade_date').reset_index(drop=True)
            vol = hist['vol']
            close = hist['close']

            long_avg = vol.iloc[-250:].mean() if len(vol) >= 250 else vol.mean()
            recent_avg_60 = vol.iloc[-60:].mean()

            if long_avg == 0:
                continue

            ratio = recent_avg_60 / long_avg
            if ratio >= 0.15:
                continue

            # 计算持续缩量天数
            threshold = long_avg * 0.25
            days = 0
            for v in reversed(vol.tolist()):
                if v < threshold:
                    days += 1
                else:
                    break

            if days < 60:
                continue

            # 价格分位（越低越好）
            recent_close = close.iloc[-250:] if len(close) >= 250 else close
            percentile = round((recent_close < close.iloc[-1]).sum() / len(recent_close) * 100, 1)

            # 综合评分
            score = round((1 - ratio) * 40 + min(days / 100, 1) * 40 + (100 - percentile) / 100 * 20, 1)

            name_val = candidates_df[candidates_df['ts_code'] == ts_code]['name'].values
            name = name_val[0] if len(name_val) > 0 else ts_code
            price = round(float(close.iloc[-1]), 2)

            results.append({
                'code': ts_code, 'name': name, 'price': price,
                'ratio': round(ratio, 3), 'days': days,
                'percentile': percentile, 'score': score
            })
            print(f"  ✓ {ts_code} {name} | {days}天 | 量比{round(ratio,3)} | 评分{score}")

            if i % 50 == 0 and i > 0:
                print(f"进度: {i}/{len(candidates)}，已发现 {len(results)} 支")

        except Exception as e:
            print(f"  × {ts_code}: {e}")
            time.sleep(0.5)
            continue

    results.sort(key=lambda x: x['score'], reverse=True)
    return results

def format_message(results, last_date):
    date_str = f"{last_date[:4]}-{last_date[4:6]}-{last_date[6:]}"
    today = datetime.now().strftime('%Y-%m-%d')

    if not results:
        return (
            f"📊 星星量扫描 {today} | 暂无信号",
            f"数据日期：{date_str}\n\n未发现持续60天以上星星量股票。"
        )

    extreme = [r for r in results if r['days'] >= 90]
    deep    = [r for r in results if 60 <= r['days'] < 90]

    lines = [f"## 📊 A股星星量预警 {today}（数据：{date_str}）\n"]
    lines.append(f"> 共发现 **{len(results)}** 支 | 🔴极度缩量(90天+): {len(extreme)}支 | 🟡深度缩量(60-90天): {len(deep)}支\n")

    if extreme:
        lines.append(f"\n### 🔴 极度缩量 TOP10（持续90天以上）\n")
        lines.append("| 代码 | 名称 | 现价 | 缩量天数 | 量比 | 价格分位 | 评分 |")
        lines.append("|------|------|------|----------|------|----------|------|")
        for r in extreme[:10]:
            lines.append(f"| {r['code']} | {r['name']} | {r['price']} | {r['days']}天 | {r['ratio']} | {r['percentile']}% | {r['score']} |")

    if deep:
        lines.append(f"\n### 🟡 深度缩量 TOP10（60-90天）\n")
        lines.append("| 代码 | 名称 | 现价 | 缩量天数 | 量比 | 价格分位 | 评分 |")
        lines.append("|------|------|------|----------|------|----------|------|")
        for r in deep[:10]:
            lines.append(f"| {r['code']} | {r['name']} | {r['price']} | {r['days']}天 | {r['ratio']} | {r['percentile']}% | {r['score']} |")

    lines.append("\n---")
    lines.append("**评分说明**：缩量强度(40分) + 持续天数(40分) + 价格低位程度(20分)")
    lines.append("\n⚠️ 量化技术筛选结果，不构成投资建议，请结合基本面自行判断。")

    return f"📊 星星量预警 {today} | 发现{len(results)}支", "\n".join(lines)

def main():
    print(f"===== 开始扫描 {datetime.now()} =====")
    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()

    last_date = get_last_trade_date(pro)
    results = screen_stocks(pro)

    title, content = format_message(results, last_date)
    print(f"\n筛选完成，共 {len(results)} 支")
    send_wechat(title, content)
    print("===== 完成 =====")

if __name__ == "__main__":
    main()
