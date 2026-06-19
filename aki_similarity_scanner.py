import concurrent.futures
import datetime as dt
import math
import os
import time

import akshare as ak
import pandas as pd
import requests


# 正样本只用来校准模型。正式筛选结果会排除这些股票。
# zone 是你确认或截图里能明显看到的“长时间低成交额平躺区”。
SAMPLES = [
    {"code": "001696", "name": "宗申动力", "zone": ("2026-03-31", "2026-06-03"), "note": "你已确认"},
    {"code": "002141", "name": "贤丰控股", "zone": None, "note": "参考样本"},
    {"code": "301303", "name": "真兰仪表", "zone": None, "note": "参考样本"},
    {"code": "000032", "name": "深桑达A", "zone": None, "note": "参考样本"},
    {"code": "603011", "name": "合锻智能", "zone": None, "note": "参考样本"},
    {"code": "300481", "name": "濮阳惠成", "zone": None, "note": "参考样本"},
    {"code": "300401", "name": "花园生物", "zone": None, "note": "参考样本"},
]
SAMPLE_CODES = {x["code"] for x in SAMPLES}

MAX_STOCKS = int(os.getenv("AKI_MAX_STOCKS", "900"))
MAX_WORKERS = int(os.getenv("AKI_MAX_WORKERS", "5"))
STOCK_LIST_SOURCE = "未获取"

# 星星量不是几天的小缩量，而是一段较长的“成交额贴地、均量线平躺”。
MIN_STAR_DAYS = 28
MAX_STAR_DAYS = 78
SCAN_LOOKBACK_DAYS = 170


# 当 GitHub 临时连不上股票列表接口时，用这个真实股票池兜底。
# 兜底池不是全市场，只是为了保证当天还能跑出一份可检查的结果，不再直接失败。
FALLBACK_STOCKS = [
    ("000001", "平安银行"), ("000002", "万科A"), ("000006", "深振业A"), ("000009", "中国宝安"),
    ("000012", "南玻A"), ("000021", "深科技"), ("000027", "深圳能源"), ("000028", "国药一致"),
    ("000031", "大悦城"), ("000034", "神州数码"), ("000035", "中国天楹"), ("000039", "中集集团"),
    ("000050", "深天马A"), ("000060", "中金岭南"), ("000063", "中兴通讯"), ("000066", "中国长城"),
    ("000069", "华侨城A"), ("000100", "TCL科技"), ("000157", "中联重科"), ("000166", "申万宏源"),
    ("000301", "东方盛虹"), ("000333", "美的集团"), ("000338", "潍柴动力"), ("000400", "许继电气"),
    ("000401", "冀东水泥"), ("000402", "金融街"), ("000408", "藏格矿业"), ("000425", "徐工机械"),
    ("000513", "丽珠集团"), ("000519", "中兵红箭"), ("000538", "云南白药"), ("000547", "航天发展"),
    ("000552", "甘肃能化"), ("000559", "万向钱潮"), ("000568", "泸州老窖"), ("000581", "威孚高科"),
    ("000596", "古井贡酒"), ("000625", "长安汽车"), ("000630", "铜陵有色"), ("000651", "格力电器"),
    ("000661", "长春高新"), ("000681", "视觉中国"), ("000703", "恒逸石化"), ("000708", "中信特钢"),
    ("000723", "美锦能源"), ("000725", "京东方A"), ("000729", "燕京啤酒"), ("000733", "振华科技"),
    ("000738", "航发控制"), ("000739", "普洛药业"), ("000750", "国海证券"), ("000768", "中航西飞"),
    ("000776", "广发证券"), ("000783", "长江证券"), ("000786", "北新建材"), ("000792", "盐湖股份"),
    ("000800", "一汽解放"), ("000807", "云铝股份"), ("000831", "中国稀土"), ("000858", "五粮液"),
    ("000876", "新希望"), ("000877", "天山股份"), ("000883", "湖北能源"), ("000895", "双汇发展"),
    ("000898", "鞍钢股份"), ("000923", "河钢资源"), ("000932", "华菱钢铁"), ("000933", "神火股份"),
    ("000938", "紫光股份"), ("000963", "华东医药"), ("000975", "银泰黄金"), ("000977", "浪潮信息"),
    ("000983", "山西焦煤"), ("000999", "华润三九"),
    ("001227", "兰州银行"), ("001286", "陕西能源"), ("001289", "龙源电力"), ("001308", "康冠科技"),
    ("001309", "德明利"), ("001872", "招商港口"), ("001979", "招商蛇口"), ("002001", "新和成"),
    ("002007", "华兰生物"), ("002008", "大族激光"), ("002025", "航天电器"), ("002027", "分众传媒"),
    ("002028", "思源电气"), ("002032", "苏泊尔"), ("002044", "美年健康"), ("002049", "紫光国微"),
    ("002050", "三花智控"), ("002064", "华峰化学"), ("002074", "国轩高科"), ("002078", "太阳纸业"),
    ("002092", "中泰化学"), ("002120", "韵达股份"), ("002129", "TCL中环"), ("002142", "宁波银行"),
    ("002151", "北斗星通"), ("002152", "广电运通"), ("002156", "通富微电"), ("002179", "中航光电"),
    ("002180", "纳思达"), ("002185", "华天科技"), ("002230", "科大讯飞"), ("002236", "大华股份"),
    ("002241", "歌尔股份"), ("002252", "上海莱士"), ("002271", "东方雨虹"), ("002273", "水晶光电"),
    ("002281", "光迅科技"), ("002294", "信立泰"), ("002304", "洋河股份"), ("002311", "海大集团"),
    ("002340", "格林美"), ("002352", "顺丰控股"), ("002371", "北方华创"), ("002384", "东山精密"),
    ("002410", "广联达"), ("002414", "高德红外"), ("002415", "海康威视"), ("002422", "科伦药业"),
    ("002459", "晶澳科技"), ("002460", "赣锋锂业"), ("002463", "沪电股份"), ("002466", "天齐锂业"),
    ("002475", "立讯精密"), ("002493", "荣盛石化"), ("002508", "老板电器"), ("002555", "三七互娱"),
    ("002558", "巨人网络"), ("002594", "比亚迪"), ("002601", "龙佰集团"), ("002603", "以岭药业"),
    ("002624", "完美世界"), ("002648", "卫星化学"), ("002709", "天赐材料"), ("002714", "牧原股份"),
    ("002736", "国信证券"), ("002812", "恩捷股份"), ("002821", "凯莱英"), ("002841", "视源股份"),
    ("002850", "科达利"), ("002916", "深南电路"), ("002920", "德赛西威"), ("002938", "鹏鼎控股"),
    ("002945", "华林证券"), ("002984", "森麒麟"),
    ("300001", "特锐德"), ("300003", "乐普医疗"), ("300014", "亿纬锂能"), ("300015", "爱尔眼科"),
    ("300024", "机器人"), ("300033", "同花顺"), ("300037", "新宙邦"), ("300059", "东方财富"),
    ("300073", "当升科技"), ("300122", "智飞生物"), ("300124", "汇川技术"), ("300136", "信维通信"),
    ("300142", "沃森生物"), ("300144", "宋城演艺"), ("300207", "欣旺达"), ("300223", "北京君正"),
    ("300274", "阳光电源"), ("300285", "国瓷材料"), ("300296", "利亚德"), ("300308", "中际旭创"),
    ("300316", "晶盛机电"), ("300347", "泰格医药"), ("300390", "天华新能"), ("300394", "天孚通信"),
    ("300408", "三环集团"), ("300413", "芒果超媒"), ("300418", "昆仑万维"), ("300433", "蓝思科技"),
    ("300450", "先导智能"), ("300454", "深信服"), ("300496", "中科创达"), ("300498", "温氏股份"),
    ("300502", "新易盛"), ("300529", "健帆生物"), ("300558", "贝达药业"), ("300568", "星源材质"),
    ("300601", "康泰生物"), ("300604", "长川科技"), ("300628", "亿联网络"), ("300661", "圣邦股份"),
    ("300672", "国科微"), ("300676", "华大基因"), ("300679", "电连技术"), ("300699", "光威复材"),
    ("300724", "捷佳伟创"), ("300750", "宁德时代"), ("300751", "迈为股份"), ("300759", "康龙化成"),
    ("300760", "迈瑞医疗"), ("300763", "锦浪科技"), ("300769", "德方纳米"), ("300782", "卓胜微"),
    ("300803", "指南针"), ("300832", "新产业"), ("300896", "爱美客"), ("300919", "中伟股份"),
    ("300957", "贝泰妮"), ("300979", "华利集团"),
    ("600000", "浦发银行"), ("600004", "白云机场"), ("600009", "上海机场"), ("600010", "包钢股份"),
    ("600011", "华能国际"), ("600015", "华夏银行"), ("600016", "民生银行"), ("600018", "上港集团"),
    ("600019", "宝钢股份"), ("600023", "浙能电力"), ("600025", "华能水电"), ("600027", "华电国际"),
    ("600028", "中国石化"), ("600029", "南方航空"), ("600030", "中信证券"), ("600031", "三一重工"),
    ("600036", "招商银行"), ("600039", "四川路桥"), ("600048", "保利发展"), ("600050", "中国联通"),
    ("600061", "国投资本"), ("600066", "宇通客车"), ("600079", "人福医药"), ("600085", "同仁堂"),
    ("600089", "特变电工"), ("600104", "上汽集团"), ("600109", "国金证券"), ("600111", "北方稀土"),
    ("600115", "中国东航"), ("600118", "中国卫星"), ("600132", "重庆啤酒"), ("600143", "金发科技"),
    ("600150", "中国船舶"), ("600153", "建发股份"), ("600160", "巨化股份"), ("600161", "天坛生物"),
    ("600170", "上海建工"), ("600176", "中国巨石"), ("600177", "雅戈尔"), ("600183", "生益科技"),
    ("600188", "兖矿能源"), ("600196", "复星医药"), ("600219", "南山铝业"), ("600233", "圆通速递"),
    ("600276", "恒瑞医药"), ("600309", "万华化学"), ("600332", "白云山"), ("600346", "恒力石化"),
    ("600352", "浙江龙盛"), ("600362", "江西铜业"), ("600369", "西南证券"), ("600372", "中航机载"),
    ("600383", "金地集团"), ("600406", "国电南瑞"), ("600415", "小商品城"), ("600426", "华鲁恒升"),
    ("600436", "片仔癀"), ("600438", "通威股份"), ("600482", "中国动力"), ("600489", "中金黄金"),
    ("600515", "海南机场"), ("600519", "贵州茅台"), ("600522", "中天科技"), ("600547", "山东黄金"),
    ("600570", "恒生电子"), ("600584", "长电科技"), ("600585", "海螺水泥"), ("600588", "用友网络"),
    ("600600", "青岛啤酒"), ("600606", "绿地控股"), ("600660", "福耀玻璃"), ("600674", "川投能源"),
    ("600690", "海尔智家"), ("600703", "三安光电"), ("600704", "物产中大"), ("600741", "华域汽车"),
    ("600745", "闻泰科技"), ("600760", "中航沈飞"), ("600795", "国电电力"), ("600809", "山西汾酒"),
    ("600837", "海通证券"), ("600845", "宝信软件"), ("600872", "中炬高新"), ("600875", "东方电气"),
    ("600886", "国投电力"), ("600887", "伊利股份"), ("600893", "航发动力"), ("600900", "长江电力"),
    ("600905", "三峡能源"), ("600918", "中泰证券"), ("600919", "江苏银行"), ("600926", "杭州银行"),
    ("600938", "中国海油"), ("600941", "中国移动"), ("600958", "东方证券"), ("600989", "宝丰能源"),
    ("600999", "招商证券"), ("601006", "大秦铁路"), ("601009", "南京银行"), ("601012", "隆基绿能"),
    ("601021", "春秋航空"), ("601066", "中信建投"), ("601088", "中国神华"), ("601100", "恒立液压"),
    ("601111", "中国国航"), ("601117", "中国化学"), ("601138", "工业富联"), ("601155", "新城控股"),
    ("601166", "兴业银行"), ("601169", "北京银行"), ("601186", "中国铁建"), ("601211", "国泰君安"),
    ("601225", "陕西煤业"), ("601229", "上海银行"), ("601236", "红塔证券"), ("601238", "广汽集团"),
    ("601288", "农业银行"), ("601318", "中国平安"), ("601319", "中国人保"), ("601328", "交通银行"),
    ("601336", "新华保险"), ("601360", "三六零"), ("601377", "兴业证券"), ("601390", "中国中铁"),
    ("601398", "工商银行"), ("601600", "中国铝业"), ("601601", "中国太保"), ("601607", "上海医药"),
    ("601618", "中国中冶"), ("601628", "中国人寿"), ("601633", "长城汽车"), ("601658", "邮储银行"),
    ("601668", "中国建筑"), ("601669", "中国电建"), ("601688", "华泰证券"), ("601696", "中银证券"),
    ("601698", "中国卫通"), ("601728", "中国电信"), ("601766", "中国中车"), ("601788", "光大证券"),
    ("601800", "中国交建"), ("601818", "光大银行"), ("601838", "成都银行"), ("601857", "中国石油"),
    ("601865", "福莱特"), ("601868", "中国能建"), ("601878", "浙商证券"), ("601881", "中国银河"),
    ("601888", "中国中免"), ("601899", "紫金矿业"), ("601901", "方正证券"), ("601916", "浙商银行"),
    ("601919", "中远海控"), ("601939", "建设银行"), ("601985", "中国核电"), ("601988", "中国银行"),
    ("601989", "中国重工"), ("601995", "中金公司"), ("601998", "中信银行"),
    ("603019", "中科曙光"), ("603185", "弘元绿能"), ("603195", "公牛集团"), ("603259", "药明康德"),
    ("603260", "合盛硅业"), ("603288", "海天味业"), ("603290", "斯达半导"), ("603369", "今世缘"),
    ("603392", "万泰生物"), ("603486", "科沃斯"), ("603501", "韦尔股份"), ("603659", "璞泰来"),
    ("603799", "华友钴业"), ("603806", "福斯特"), ("603833", "欧派家居"), ("603899", "晨光股份"),
    ("603986", "兆易创新"), ("603993", "洛阳钼业"),
]


def push_server_chan(title, text):
    key = os.getenv("AKI_SERVER_CHAN_SENDKEY")
    if not key:
        raise RuntimeError("没有设置 AKI_SERVER_CHAN_SENDKEY")
    url = f"https://sctapi.ftqq.com/{key}.send"
    response = requests.post(url, data={"title": title, "desp": text}, timeout=30)
    print(response.text)
    response.raise_for_status()


def fetch_history(code):
    end = dt.date.today().strftime("%Y%m%d")
    start = (dt.date.today() - dt.timedelta(days=620)).strftime("%Y%m%d")
    prefix = "sh" if code.startswith("6") else "sz"
    symbol = f"{prefix}{code}"

    errors = []
    # 东方财富日线通常带“换手率”，所以优先用它；拿不到再用其他源。
    try:
        return ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq", timeout=20)
    except Exception as exc:
        errors.append(f"东财:{exc}")
    try:
        return ak.stock_zh_a_daily(symbol=symbol, start_date=start, end_date=end, adjust="qfq")
    except Exception as exc:
        errors.append(f"新浪:{exc}")
    try:
        return ak.stock_zh_a_hist_tx(symbol=symbol, start_date=start, end_date=end, adjust="qfq", timeout=20)
    except Exception as exc:
        errors.append(f"腾讯:{exc}")
    raise RuntimeError(" / ".join(errors))


def prepare(df):
    rename_map = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "换手率": "turnover",
        "date": "date",
        "open": "open",
        "close": "close",
        "high": "high",
        "low": "low",
        "volume": "volume",
        "amount": "amount",
        "turnover": "turnover",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    if "date" not in df.columns:
        df = df.reset_index().rename(columns={"index": "date"})

    for col in ["open", "close", "high", "low", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    else:
        df["amount"] = df["volume"] * df["close"]
    if "turnover" in df.columns:
        df["turnover"] = pd.to_numeric(df["turnover"], errors="coerce")
    else:
        df["turnover"] = float("nan")

    # 有的接口成交额单位不同。这里不做绝对金额判断，只做同一只股票内部比例。
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "open", "close", "high", "low", "volume", "amount"])
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    df = df[df["amount"] > 0].reset_index(drop=True)

    df["a3"] = df["amount"].rolling(3).mean()
    df["a5"] = df["amount"].rolling(5).mean()
    df["a10"] = df["amount"].rolling(10).mean()
    df["a20"] = df["amount"].rolling(20).mean()
    df["a60"] = df["amount"].rolling(60).mean()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    return df


def safe_ratio(a, b):
    if b is None or pd.isna(b) or abs(float(b)) < 1e-12:
        return 0.0
    return float(a) / float(b)


def linear_slope_ratio(values):
    values = pd.Series(values).dropna()
    if len(values) < 8:
        return 0.0
    y = values.astype(float).to_list()
    n = len(y)
    x_mean = (n - 1) / 2
    y_mean = sum(y) / n
    denom = sum((i - x_mean) ** 2 for i in range(n))
    if denom == 0 or abs(y_mean) < 1e-12:
        return 0.0
    slope = sum((i - x_mean) * (y[i] - y_mean) for i in range(n)) / denom
    return slope * n / y_mean


def date_index(df, day):
    day = pd.to_datetime(day)
    hits = df.index[df["date"] >= day].tolist()
    return hits[0] if hits else None


def feature_from_window(df, start, length):
    end = start + length - 1
    if start < 80 or end >= len(df):
        return None

    win = df.iloc[start : end + 1]
    before = df.iloc[max(0, start - 90) : start]
    context = df.iloc[max(0, start - 90) : end + 1]
    after = df.iloc[end + 1 :]
    if len(win) < MIN_STAR_DAYS or len(context) < 90:
        return None

    star_low = float(win["low"].min())
    star_high = float(win["high"].max())
    context_low = float(context["low"].min())
    context_high = float(context["high"].max())
    win_amount_mid = float(win["amount"].median())
    context_amount_mid = float(context["amount"].median())
    before_amount_mid = float(before["amount"].median()) if len(before) >= 20 else context_amount_mid

    a5 = win["a5"].dropna()
    a10 = win["a10"].dropna()
    amount_line = a5 if len(a5) >= 12 else win["amount"]
    amount_slope = linear_slope_ratio(amount_line)
    amount_flatness = safe_ratio(amount_line.quantile(0.90), amount_line.quantile(0.25))
    price_slope = linear_slope_ratio(win["close"])

    low_line_days = float((win["amount"] <= context["amount"].quantile(0.45)).mean())
    calm_days = float((win["amount"] <= context["amount"].quantile(0.60)).mean())
    red_spike_days = float((win["amount"] >= context["amount"].quantile(0.82)).mean())

    pos = safe_ratio(win["close"].iloc[-1] - context_low, context_high - context_low)
    tight = safe_ratio(star_high - star_low, star_low)
    amount_ratio = safe_ratio(win_amount_mid, context_amount_mid)
    before_ratio = safe_ratio(win_amount_mid, before_amount_mid)
    turnover_known = bool(win["turnover"].notna().mean() >= 0.80)
    if turnover_known:
        turnover_median = float(win["turnover"].median())
        turnover_low_days = float((win["turnover"] <= 4.0).mean())
        turnover_high_days = float((win["turnover"] > 4.0).mean())
    else:
        turnover_median = float("nan")
        turnover_low_days = 0.0
        turnover_high_days = 1.0
    gain_to_now = safe_ratio(df["close"].iloc[-1], star_low) - 1
    days_after = len(df) - 1 - end

    if len(after) > 0:
        after10 = after.head(10)
        after25 = after.head(25)
        launch_amount = safe_ratio(after10["amount"].max(), win_amount_mid)
        gain_after_25 = safe_ratio(after25["close"].max(), star_low) - 1
        launch_days = int((after10["amount"] >= win_amount_mid * 1.8).sum())
    else:
        launch_amount = 0.0
        gain_after_25 = 0.0
        launch_days = 0

    pct5 = safe_ratio(df["close"].iloc[-1], df["close"].iloc[-6]) - 1 if len(df) >= 6 else 0.0
    pct20 = safe_ratio(df["close"].iloc[-1], df["close"].iloc[-21]) - 1 if len(df) >= 21 else 0.0
    current_amount_ratio = safe_ratio(df["a5"].iloc[-1], win_amount_mid)
    current_price_pos = safe_ratio(df["close"].iloc[-1] - context_low, context_high - context_low)

    return {
        "start_idx": start,
        "end_idx": end,
        "start": win["date"].iloc[0].strftime("%Y-%m-%d"),
        "end": win["date"].iloc[-1].strftime("%Y-%m-%d"),
        "length": length,
        "pos": pos,
        "current_price_pos": current_price_pos,
        "tight": tight,
        "amount_ratio": amount_ratio,
        "before_ratio": before_ratio,
        "turnover_known": turnover_known,
        "turnover_median": turnover_median,
        "turnover_low_days": turnover_low_days,
        "turnover_high_days": turnover_high_days,
        "low_line_days": low_line_days,
        "calm_days": calm_days,
        "red_spike_days": red_spike_days,
        "amount_slope": amount_slope,
        "amount_flatness": amount_flatness,
        "price_slope": price_slope,
        "gain_to_now": gain_to_now,
        "gain_after_25": gain_after_25,
        "launch_amount": launch_amount,
        "launch_days": launch_days,
        "current_amount_ratio": current_amount_ratio,
        "pct5": pct5,
        "pct20": pct20,
        "days_after": days_after,
    }


def base_star_filter(f):
    # 长时间低成交额、均量线贴地，价格不能已经大幅脱离低位。
    return (
        MIN_STAR_DAYS <= f["length"] <= MAX_STAR_DAYS
        and f["amount_ratio"] <= 0.62
        and f["before_ratio"] <= 0.72
        and f["turnover_known"]
        and f["turnover_median"] <= 4.0
        and f["turnover_low_days"] >= 0.80
        and f["low_line_days"] >= 0.56
        and f["calm_days"] >= 0.72
        and f["red_spike_days"] <= 0.18
        and f["amount_flatness"] <= 2.80
        and f["amount_slope"] <= 0.38
        and f["tight"] <= 0.50
        and f["pos"] <= 0.72
    )


def score_long_star(f):
    score = 100.0
    score += min(f["length"], 70) * 0.45
    score += max(0, 0.62 - f["amount_ratio"]) * 55
    score += max(0, 0.72 - f["before_ratio"]) * 35
    score += max(0, 4.0 - f["turnover_median"]) * 9
    score += f["turnover_low_days"] * 30
    score += f["low_line_days"] * 35
    score += f["calm_days"] * 25
    score += max(0, 2.8 - f["amount_flatness"]) * 12
    score -= max(0, f["amount_slope"]) * 25
    score -= max(0, f["tight"] - 0.34) * 60
    score -= f["red_spike_days"] * 55
    return score


def sample_like_score(f, template):
    keys = {
        "length": 0.018,
        "amount_ratio": 1.5,
        "before_ratio": 1.1,
        "turnover_median": 0.22,
        "turnover_low_days": 0.8,
        "low_line_days": 1.0,
        "amount_flatness": 0.34,
        "amount_slope": 0.85,
        "tight": 0.95,
        "pos": 0.55,
        "price_slope": 0.45,
    }
    dist = 0.0
    for key, weight in keys.items():
        a = f.get(key)
        b = template.get(key)
        if pd.isna(a) or pd.isna(b):
            dist += weight * 2.0
        else:
            dist += weight * abs(float(a) - float(b))
    return max(0.0, 100.0 - dist * 42.0)


def classify_window(f):
    # 已经开始涨：必须从长低量区之后，出现放量和价格脱离。
    launched = (
        1 <= f["days_after"] <= 70
        and f["gain_to_now"] >= 0.22
        and f["gain_after_25"] >= 0.18
        and f["launch_amount"] >= 1.80
        and f["launch_days"] >= 1
    )
    # 还趴着：窗口要接近现在，成交额仍低，价格还没明显启动。
    ready = (
        f["days_after"] <= 4
        and f["gain_to_now"] <= 0.16
        and f["pct20"] <= 0.16
        and f["current_amount_ratio"] <= 1.45
        and f["current_price_pos"] <= 0.74
    )
    if launched:
        return "已开始涨验证组"
    if ready:
        return "仍在星星量低位组"
    return None


def find_best_long_window(df, templates=None, fixed_zone=None, sample_mode=False):
    if fixed_zone:
        start = date_index(df, fixed_zone[0])
        end = date_index(df, fixed_zone[1])
        if start is None or end is None or end <= start:
            return None
        f = feature_from_window(df, start, end - start + 1)
        if f:
            f["fixed_zone"] = True
        return f

    best = None
    min_start = max(80, len(df) - SCAN_LOOKBACK_DAYS)
    max_start = max(min_start, len(df) - MIN_STAR_DAYS)
    for length in range(MIN_STAR_DAYS, MAX_STAR_DAYS + 1):
        for start in range(min_start, max_start + 1):
            if start + length > len(df):
                continue
            f = feature_from_window(df, start, length)
            if not f or not base_star_filter(f):
                continue
            group = classify_window(f)
            if not sample_mode and group is None:
                continue

            if templates:
                scores = [(sample_like_score(f, t), t["sample_name"]) for t in templates]
                similarity, sample_name = max(scores, key=lambda x: x[0])
                if similarity < 70:
                    continue
            else:
                similarity, sample_name = 100.0, "样本"

            score = score_long_star(f) + similarity * 0.70
            if group == "已开始涨验证组":
                score += min(f["gain_to_now"], 1.2) * 25 + min(f["launch_amount"], 6) * 3
            elif group == "仍在星星量低位组":
                score += max(0, 1.45 - f["current_amount_ratio"]) * 18

            f = dict(f)
            f["group"] = group or "样本模板"
            f["similarity"] = similarity
            f["sample_name"] = sample_name
            f["score"] = score
            f["fixed_zone"] = False
            if best is None or f["score"] > best["score"]:
                best = f
    return best


def build_templates():
    templates = []
    lines = []
    for sample in SAMPLES:
        code = sample["code"]
        name = sample["name"]
        try:
            df = prepare(fetch_history(code))
            f = find_best_long_window(df, fixed_zone=sample["zone"], sample_mode=True)
            if f is None:
                f = find_best_long_window(df, sample_mode=True)
            if f:
                f = dict(f)
                f["sample_code"] = code
                f["sample_name"] = name
                templates.append(f)
                tag = "人工校准" if sample["zone"] else "程序参考"
                lines.append(
                    f"- {name}({code})：{tag}，星星量长区 {f['start']} 至 {f['end']}，"
                    f"{f['length']}个交易日，换手中位{f['turnover_median']:.2f}%，低于4%天数{f['turnover_low_days']:.0%}，"
                    f"成交额比{f['amount_ratio']:.2f}，后涨幅{f['gain_to_now']:.1%}"
                )
            else:
                lines.append(f"- {name}({code})：没有建立模板")
        except Exception as exc:
            lines.append(f"- {name}({code})：数据失败 {exc}")
        time.sleep(0.08)
    return templates, lines


def get_stock_list():
    global STOCK_LIST_SOURCE
    errors = []
    try:
        d = ak.stock_zh_a_spot_em()
        d = d.rename(columns={"代码": "code", "名称": "name", "最新价": "price", "总市值": "cap"})
        d["code"] = d["code"].astype(str).str.zfill(6)
        d["name"] = d["name"].astype(str)
        d["price"] = pd.to_numeric(d["price"], errors="coerce")
        d["cap_yi"] = pd.to_numeric(d["cap"], errors="coerce") / 100000000
        d = d[~d["name"].str.contains("ST|退", case=False, na=False)]
        d = d[~d["code"].str.startswith(("8", "4", "9"))]
        d = d[(d["price"] >= 2.5) & (d["price"] <= 180)]
        d = d[(d["cap_yi"].isna()) | ((d["cap_yi"] >= 8) & (d["cap_yi"] <= 3000))]
        STOCK_LIST_SOURCE = "东方财富实时全市场列表"
        return d[["code", "name"]].drop_duplicates("code").head(MAX_STOCKS).to_dict("records")
    except Exception as exc:
        errors.append(f"东方财富实时列表失败：{exc}")

    try:
        d = ak.stock_info_a_code_name()
        d = d.rename(columns={"code": "code", "name": "name", "代码": "code", "名称": "name"})
        d["code"] = d["code"].astype(str).str.zfill(6)
        d["name"] = d["name"].astype(str)
        d = d[~d["name"].str.contains("ST|退", case=False, na=False)]
        d = d[~d["code"].str.startswith(("8", "4", "9"))]
        STOCK_LIST_SOURCE = "A股代码名称列表"
        return d[["code", "name"]].drop_duplicates("code").head(MAX_STOCKS).to_dict("records")
    except Exception as exc:
        errors.append(f"A股代码名称列表失败：{exc}")

    frames = []
    for fn, kwargs, code_col, name_col, label in [
        (ak.stock_info_sh_name_code, {"symbol": "主板A股"}, "证券代码", "证券简称", "上交所主板"),
        (ak.stock_info_sh_name_code, {"symbol": "科创板"}, "证券代码", "证券简称", "上交所科创板"),
        (ak.stock_info_sz_name_code, {"symbol": "A股列表"}, "A股代码", "A股简称", "深交所A股"),
    ]:
        try:
            x = fn(**kwargs).rename(columns={code_col: "code", name_col: "name"})
            frames.append(x[["code", "name"]])
        except Exception as exc:
            errors.append(f"{label}失败：{exc}")

    if frames:
        d = pd.concat(frames, ignore_index=True)
        d["code"] = d["code"].astype(str).str.split(".", expand=True).iloc[:, 0].str.zfill(6)
        d["name"] = d["name"].astype(str)
        d = d[~d["name"].str.contains("ST|退", case=False, na=False)]
        d = d[~d["code"].str.startswith(("8", "4", "9"))]
        STOCK_LIST_SOURCE = "交易所股票列表"
        return d.drop_duplicates("code")[["code", "name"]].head(MAX_STOCKS).to_dict("records")

    STOCK_LIST_SOURCE = "内置真实备用股票池"
    print("股票列表接口全部失败，改用内置真实备用股票池：", "；".join(errors))
    return [{"code": code, "name": name} for code, name in FALLBACK_STOCKS[:MAX_STOCKS]]


def scan_item(item, templates):
    code = item["code"]
    name = item["name"]
    try:
        df = prepare(fetch_history(code))
        if len(df) < 140:
            return None
        f = find_best_long_window(df, templates=templates)
        if not f:
            return None
        f = dict(f)
        f["code"] = code
        f["name"] = name
        f["close"] = float(df["close"].iloc[-1])
        return f
    except Exception as exc:
        print(code, name, "失败", exc)
        return None


def scan_market(templates):
    items = [x for x in get_stock_list() if x["code"] not in SAMPLE_CODES]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {executor.submit(scan_item, item, templates): item for item in items}
        for i, future in enumerate(concurrent.futures.as_completed(future_map), 1):
            result = future.result()
            if result:
                results.append(result)
            if i % 200 == 0:
                print(f"已扫描 {i}/{len(items)}，命中 {len(results)}")
    return items, results


def format_item(x, idx):
    return (
        f"{idx}. {x['name']}({x['code']}) 收{x['close']:.2f}，评分{x['score']:.1f}，相似{x['sample_name']} {x['similarity']:.0f}%\n"
        f"   星星量长区：{x['start']} 至 {x['end']}，{x['length']}个交易日，之后{int(x['days_after'])}天\n"
        f"   形态：换手中位{x['turnover_median']:.2f}%，低于4%天数{x['turnover_low_days']:.0%}，成交额比{x['amount_ratio']:.2f}，低量天数{x['low_line_days']:.0%}\n"
        f"   平稳：成交额平稳{x['amount_flatness']:.2f}，振幅{x['tight']:.1%}\n"
        f"   状态：后涨幅{x['gain_to_now']:.1%}，5日{x['pct5']:.1%}，20日{x['pct20']:.1%}，启动放量{x['launch_amount']:.1f}倍"
    )


def main():
    templates, sample_lines = build_templates()
    if not templates:
        raise RuntimeError("样本模板没有建立成功，停止扫描")

    items, results = scan_market(templates)
    launched = sorted([x for x in results if x["group"] == "已开始涨验证组"], key=lambda x: x["score"], reverse=True)[:25]
    ready = sorted([x for x in results if x["group"] == "仍在星星量低位组"], key=lambda x: x["score"], reverse=True)[:25]

    title = "AKI星星量长区筛选 " + dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# {title}",
        "",
        "说明：这版按“长时间低成交额平躺区”筛，不再把几天的小缩量当星星量。样本只建模板，正式结果已排除样本股；不是买卖建议。",
        f"本次股票池：{STOCK_LIST_SOURCE}。",
        "",
        "## 样本模板验证",
    ]
    lines.extend(sample_lines)
    lines.append("")

    for group, rows in [("正式筛选：已开始涨验证组", launched), ("正式筛选：仍在星星量低位组", ready)]:
        lines.append(f"## {group}（{len(rows)}只）")
        lines.extend(["今天没有命中。"] if not rows else [format_item(x, i) for i, x in enumerate(rows, 1)])
        lines.append("")

    lines.append(f"正式扫描 {len(items)} 只，命中 {len(results)} 只。")
    text = "\n".join(lines)
    print(text)
    push_server_chan(title, text)


if __name__ == "__main__":
    main()
