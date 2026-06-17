import unittest

import numpy as np
import pandas as pd

from AKI_star_volume_scanner import StockMeta, latest_signal


def fake_history(breakout: bool) -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=170, freq="B")
    down = np.linspace(24, 12, 120)
    base = np.linspace(12.2, 13.0, 30)
    star = np.linspace(12.6, 12.9, 10)
    if breakout:
        tail = np.linspace(13.1, 18.2, 10)
    else:
        tail = np.linspace(12.8, 13.2, 10)
    close = np.concatenate([down, base, star, tail])
    open_ = close * 0.995
    high = close * 1.025
    low = close * 0.975
    volume = np.concatenate([
        np.linspace(900000, 500000, 120),
        np.linspace(600000, 420000, 30),
        np.full(10, 90000),
        np.linspace(180000, 900000, 10) if breakout else np.full(10, 95000),
    ])
    return pd.DataFrame(
        {
            "日期": dates,
            "开盘": open_,
            "收盘": close,
            "最高": high,
            "最低": low,
            "成交量": volume,
            "换手率": 2.1,
        }
    )


class StarVolumeScannerTest(unittest.TestCase):
    def test_breakout_signal(self):
        signal = latest_signal(StockMeta(code="001696", name="宗申动力"), fake_history(True), {})
        self.assertIsNotNone(signal)
        self.assertEqual(signal.group, "已暴涨验证组")

    def test_candidate_signal(self):
        signal = latest_signal(StockMeta(code="000001", name="测试股票"), fake_history(False), {})
        self.assertIsNotNone(signal)
        self.assertEqual(signal.group, "有暴涨趋势组")


if __name__ == "__main__":
    unittest.main()
