import akshare as ak
import pandas as pd
import requests
import os
import time
from datetime import datetime, timedelta

SEVER_CHAN_KEY = os.environ.get('SEVER_CHAN_KEY', '')

def send_wechat(title, content):
    url = f"https://sctapi.ftqq.com/{SEVER_CHAN_KEY}.send"
    data = {"title": title, "desp": content}
    try:
        resp = requests.post(url, data=data, timeout=10)
        print(f"推送结果: {resp.status_code}")
    except Exception as e:
        print(f"推送失败: {e}")

def analyze_stock(code, spot_df):
    try:
        hist = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=(datetime.now() - timedelta(days=450)).strftime('%Y%m%d'),
            end_date=datetime.now().strftime('%Y%m%d'),
            adjust="qfq"
        )
        if hist is None or len(hist) < 130:
            return None

        vol = hist['成交量']
        close = hist['收盘']

        long_avg = vol.iloc[-250:].mean() if len(vol) >= 250 else vol.mean()
        recent_avg_60 = vol.iloc[-60:].mean()
        recent_avg_20 = vol.iloc[-20:].mean()

        if long_avg == 0:
            return None

        ratio = recent_avg_60 / long_avg

        if ratio >= 0.15:
            return None

        # 计算持续缩量天数
        threshold = long_avg * 0.25
        days = 0
        for v in reversed(vol.tolist()):
            if v < threshold:
                days += 1
            else:
                break

        if days < 60:
            return None

        # 价格分位（越低越好）
        recent_close = close.iloc[-250:] if len(close) >= 250 else close
        percentile = round((recent_close < close.iloc[-1]).sum() / len(recent_close) * 100, 1)

        # 综合评分
        score = round((1 - ratio) * 40 + min(days / 100, 1) * 40 + (100 - percentile) / 100 * 20, 1)

        name_arr = spot_df[spot_df['代码'] == code]['名称'].values
        name = name_arr[0] if len(name_arr) > 0 else code
        price = round(close.iloc[-1], 2)

        return {
            'code': code, 'name': name, 'price': price,
            'ratio': round(ratio, 3), 'days': days,
            'percentile': percentile, 'score': score
        }
    except Exception as e:
        return None

def screen_stocks():
    print("获取全市场行情...")
    try:
        spot_df = ak.stock_zh_a_spot_em()
    except Exception as e:
        print(f"获取失败: {e}")
        return []

    # 过滤ST、退市、北交所、科创板
    spot_df = spot_df[~spot_df['名称'].str.contains('ST|退', na=False)]
    spot_df = spot_df[~spot_df['代码'].str.startswith(('688', '689', '8', '4', '9'))]
    spot_df = spot_df.reset_index(drop=True)

    # 初筛：今日量比极小的（量比<0.3）
    if '量比' in spot_df.columns:
        spot_df['量比'] = pd.to_numeric(spot_df['量比'], errors='coerce').fillna(1)
        candidates = spot_df[spot_df['量比'] < 0.3]['代码'].tolist()
    else:
        candidates = spot_df['代码'].tolist()

    print(f"初筛候选: {len(candidates)} 支，开始深度分析...")

    results = []
    limit = min(len(candidates), 400)

    for i, code in enumerate(candidates[:limit]):
        result = analyze_stock(code, spot_df)
        if result:
            results.append(result)
            print(f"  ✓ 发现: {result['code']} {result['name']} | {result['days']}天 | 评分{result['score']}")

        if i % 30 == 0 and i > 0:
            print(f"进度: {i}/{limit}，已发现 {len(results)} 支")

        time.sleep(0.15)

    results.sort(key=lambda x: x['score'], reverse=True)
    return results

def format_message(results):
    today = datetime.now().strftime('%Y-%m-%d')

    if not results:
        title = f"📊 星星量扫描 {today} | 暂无信号"
        content = "今日未发现持续60天以上星星量股票，市场活跃度偏高或筛选条件较严。"
        return title, content

    extreme = [r for r in results if r['days'] >= 90]
    deep    = [r for r in results if 60 <= r['days'] < 90]

    lines = [f"## 📊 A股星星量预警 {today}\n"]
    lines.append(f"> 共发现 **{len(results)}** 支 | 极度缩量(90天+): {len(extreme)}支 | 深度缩量(60-90天): {len(deep)}支\n")

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
    lines.append("**评分说明**：综合缩量强度(40分)+持续天数(40分)+价格低位程度(20分)")
    lines.append("\n⚠️ 以上为量化技术筛选结果，不构成投资建议，请结合基本面自行判断。")

    title = f"📊 星星量预警 {today} | 发现{len(results)}支"
    content = "\n".join(lines)
    return title, content

def main():
    print(f"===== 开始扫描 {datetime.now()} =====")
    results = screen_stocks()
    title, content = format_message(results)
    print(f"\n筛选完成，共 {len(results)} 支")
    print(f"推送标题: {title}")
    send_wechat(title, content)
    print("===== 完成 =====")

if __name__ == "__main__":
    main()
