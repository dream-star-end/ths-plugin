# -*- coding: utf-8 -*-
"""compute_indicators 单元测试（合成数据，纯标准库）。

运行：python3 -m unittest discover -s tests -v
"""
import importlib.util
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("ths", os.path.join(_ROOT, "ths.py"))
ths = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ths)


def rows_from_closes(closes):
    return [{"date": f"202601{i:02d}", "open": c, "high": c, "low": c,
             "close": c, "volume": 1000.0, "amount": 1000.0 * c,
             "turnover_pct": None} for i, c in enumerate(closes, 1)]


class TestIndicators(unittest.TestCase):
    def test_flat_series(self):
        out = ths.compute_indicators(rows_from_closes([10.0] * 100))
        last = out[-1]
        self.assertEqual(last["ma5"], 10.0)
        self.assertEqual(last["ma60"], 10.0)
        self.assertAlmostEqual(last["dif"], 0.0, places=6)
        self.assertAlmostEqual(last["macd_hist"], 0.0, places=6)
        # 无波动时 RSV 取中值 50，K/D/J 收敛到 50
        self.assertAlmostEqual(last["kdj_k"], 50.0, places=1)
        self.assertAlmostEqual(last["kdj_d"], 50.0, places=1)
        self.assertEqual(last["boll_upper"], last["boll_lower"])

    def test_uptrend(self):
        closes = [100.0 + i for i in range(100)]
        out = ths.compute_indicators(rows_from_closes(closes))
        last = out[-1]
        # 线性上升：MA5 应等于最后 5 根收盘均值
        self.assertAlmostEqual(last["ma5"], sum(closes[-5:]) / 5)
        self.assertGreater(last["ma5"], last["ma20"])
        self.assertGreater(last["dif"], 0)
        self.assertGreater(last["macd_hist"], 0)
        # 单调上涨 RSI 应为 100；匀速线性上涨时 RSV 恒为 100，
        # K/D/J 收敛到 100（J 冲过 100 需要 K 快速上穿 D 的加速形态）
        self.assertAlmostEqual(last["rsi14"], 100.0, places=3)
        self.assertAlmostEqual(last["kdj_k"], 100.0, places=1)
        self.assertAlmostEqual(last["kdj_j"], 100.0, places=1)

    def test_warmup_nones(self):
        out = ths.compute_indicators(rows_from_closes([10.0] * 30))
        self.assertIsNone(out[0]["ma5"])
        self.assertIsNone(out[3]["kdj_k"])
        self.assertIsNone(out[10]["rsi14"])
        self.assertIsNotNone(out[29]["ma20"])


class TestNormalize(unittest.TestCase):
    def test_stock_codes(self):
        self.assertEqual(ths._normalize_code("600519"), ("600519", "17"))
        self.assertEqual(ths._normalize_code("000001.SZ"), ("000001", "33"))
        self.assertEqual(ths._normalize_code("920185"), ("920185", "151"))

    def test_index_code_rejected(self):
        with self.assertRaises(ValueError):
            ths._normalize_code("1A0001")


if __name__ == "__main__":
    unittest.main()
