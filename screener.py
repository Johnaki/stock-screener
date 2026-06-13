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

def is_valid_code(ts_code):
    code = ts_code.split('.')[0]
    # 排除科创板(688/689)、北交所、退市
    if code.startswith(('688', '689', '83', '87', '43', '40', '92', '93')):
        return False
    return ts_code.endswith('.SZ') or ts_code.endswith('.SH')

def safe_call(func, **kwargs):
    for attempt in range(3):
        try:
            result = func(**kwargs)
            if result is not None and len(result) > 0:
                return result
            return result
        except Exception as e:
            msg = str(e)
            if '频率超限' in msg:
                wait = 65 if '分钟' in msg else 3610
                print(f"频率超限，等待{wait}秒... ({attempt+1}/3)")
                time.sleep(wait)
            else:
                print(f"错误: {e}")
                time.sleep(5)
    return None

def screen_stocks(pro):
    last_date = get_last_trade_date(pro)
    time.sleep(3)

    print("获取全市场行情...")
    daily_all = safe_call(pro.daily, trade_date=last_date)

    if daily_all is None or len(daily_all) == 0:
        print("获取行情失败")
        return [], last_date

    daily_all = daily_all[daily_all['ts_code'].apply(is_valid_code)]
    daily_all = daily_all[pd.to_numeric(daily_all['vol'], errors='coerce') > 0]
    print(f"有效股票: {len(daily_all)} 支")

    candidates_df = daily_all.nsmallest(300, 'vol')
    candidates = candidates_df['ts_code'].tolist()
    print(f"初筛候选: {len(candidates)} 支，开始深度分析...")

    results = []
    start_date = (datetime.now() - timedelta(days=450)).strftime('%Y%m%d')

    for i, ts_code in enumerate(candidates):
        try:
            hist = safe_call(pro.daily, ts_code=ts_code,
                             start_date=start_date, end_date=last_date)
            time.sleep(0.5)

            if hist is None or len(hist) < 130:
                continue

            hist = hist.sort_values('trade_date').reset_index(drop=True)
            vol   = hist['vol']
            close = hist['close']

            long_avg       = vol.iloc[-250:].mean() if len(vol) >= 250 else vol.mean()
            recent_avg_60  = vol.iloc[-60:].mean()

            if long_avg == 0:
                continue

            ratio = recent_avg_60 / long_avg
            if ratio >= 0.15:
                continue

            threshold = long_avg * 0.25
            days = 0
            for v in reversed(vol.tolist()):
                if v < threshold:
                    days += 1
                else:
                    break

            if days < 60:
                continue

            recent_close = close.iloc[-250:] if len(close) >= 250 else close
            percentile   = round((recent_close < close.iloc[-1]).sum() / len(recent_close) * 100, 1)
            score        = round((1 - ratio) * 40 + min(days / 100, 1) * 40 + (100 - percentile) / 100 * 20, 1)
            price        = round(float(close.iloc[-1]), 2)

            results.append({
                'code': ts_code, 'price': price,
                'ratio': round(ratio, 3), 'days': days,
                'percentile': percentile, 'score': score
            })
            print(f"  ✓ {ts_code} | {days}天 | 量比{round(ratio,3)} | 评分{score}")

            if i % 50 == 0 and i > 0:
                print(f"进度: {i}/{len(candidates)}，已发现{len(results)}支")

        except Exception as e:
            print(f"  × {ts_code}: {e}")
            time.sleep(1)

    results.sort(key=lambda x: x['score'], reverse=True)
    return results, last_date

def format_message(results, last_date):
    date_str = f"{last_date[:4]}-{last_date[4:6]}-{last_date[6:]}"
    today    = datetime.now().strftime('%Y-%m-%d')

    if not results:
        return (
            f"📊 星星量扫描 {today} | 暂无信号",
            f"数据日期：{date_str}\n\n未发现持续60天以上星星量股票。"
        )

    extreme = [r for r in results if r['days'] >= 90]
    deep    = [r for r in results if 60 <= r['days'] < 90]

    lines = [f"## 📊 A股星星量预警 {today}（数据：{date_str}）\n"]
    lines.append(f"> 共发现 **{len(results)}** 支 | 🔴极度(90天+): {len(extreme)}支 | 🟡深度(60-90天): {len(deep)}支\n")

    if extreme:
        lines.append("\n### 🔴 极度缩量 TOP10（90天以上）\n")
        lines.append("| 代码 | 现价 | 缩量天数 | 量比 | 价格分位 | 评分 |")
        lines.append("|------|------|----------|------|----------|------|")
        for r in extreme[:10]:
            lines.append(f"| {r['code']} | {r['price']} | {r['days']}天 | {r['ratio']} | {r['percentile']}% | {r['score']} |")

    if deep:
        lines.append("\n### 🟡 深度缩量 TOP10（60-90天）\n")
        lines.append("| 代码 | 现价 | 缩量天数 | 量比 | 价格分位 | 评分 |")
        lines.append("|------|------|----------|------|----------|------|")
        for r in deep[:10]:
            lines.append(f"| {r['code']} | {r['price']} | {r['days']}天 | {r['ratio']} | {r['percentile']}% | {r['score']} |")

    lines.append("\n---")
    lines.append("**评分**：缩量强度(40) + 持续天数(40) + 价格低位(20)")
    lines.append("\n⚠️ 量化筛选结果，不构成投资建议。")

    return f"📊 星星量预警 {today} | 发现{len(results)}支", "\n".join(lines)

def main():
    print(f"===== 开始扫描 {datetime.now()} =====")
    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()
    results, last_date = screen_stocks(pro)
    title, content = format_message(results, last_date)
    print(f"\n筛选完成，共 {len(results)} 支")
    send_wechat(title, content)
    print("===== 完成 =====")

if __name__ == "__main__":
    main()
