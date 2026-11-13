"""补报/更正接入与固定基准重算的单元测试（纯标准库，python3 -m unittest 即可运行）。

运行：
    cd backend && python3 -m unittest discover -s ../test-artifacts/gb-1020/tests -p 'test_*.fv' -v
或直接：
    python3 test-artifacts/gb-1020/tests/test_business.py
"""
import math
import os
import statistics
import sys
import unittest
from datetime import date, timedelta

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'backend'))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from src.services.ingest import (  # noqa: E402
    initial_ingest, merge_ingest, summarize, validate_row,
    REASON_DUPLICATE, REASON_CONFLICT, REASON_OLD,
)

CHANNELS = ('新闻一频', '文化二频', '城市直播')
START = date(2026, 7, 1)


def row(day_offset, channel, interruption=3.0, planned=720.0, **overrides):
    day = str(START + timedelta(days=day_offset))
    base = {
        'day': day, 'channel': channel, 'planned_min': planned,
        'aired_min': round(planned - interruption, 2),
        'interruption_min': interruption, 'reach': 150000,
    }
    base.update(overrides)
    return base


def series(days, channel='新闻一频', interruption=3.0):
    return [row(i, channel, interruption) for i in range(days)]


class ValidateRowTests(unittest.TestCase):
    def test_valid_row_normalized(self):
        clean, reason = validate_row(row(0, '新闻一频'))
        self.assertIsNone(reason)
        self.assertEqual(clean['day'], str(START))
        self.assertEqual(clean['channel'], '新闻一频')

    def test_missing_field_rejected_with_label(self):
        bad = row(1, '新闻一频')
        del bad['planned_min']
        clean, reason = validate_row(bad)
        self.assertIsNone(clean)
        self.assertIn('计划分钟', reason)

    def test_non_finite_and_boolean_rejected(self):
        for value in ('abc', float('inf'), float('nan'), True):
            bad = row(2, '新闻一频')
            bad['planned_min'] = value
            clean, reason = validate_row(bad)
            self.assertIsNone(clean, value)
            self.assertIn('有限数值', reason)

    def test_domain_bounds_rejected(self):
        cases = [
            ({'planned_min': 0}, '计划分钟'),
            ({'planned_min': 1441}, '计划分钟'),
            ({'aired_min': 800}, '实播分钟'),
            ({'interruption_min': 721}, '停播分钟'),
        ]
        for override, label in cases:
            clean, reason = validate_row(row(3, '新闻一频', **override))
            self.assertIsNone(clean)
            self.assertIn(label, reason)

    def test_bad_day_and_blank_channel(self):
        self.assertIsNotNone(validate_row(row(4, '新闻一频', day='2026-13-40'))[1])
        bad = row(4, '新闻一频')
        bad['channel'] = '   '
        self.assertIn('链路', validate_row(bad)[1])


class InitialIngestTests(unittest.TestCase):
    def test_clean_batch_accepted_and_logged(self):
        incoming = series(70)
        valid, rejected, log = initial_ingest(incoming, 'seed.csv')
        self.assertEqual(len(valid), 70)
        self.assertEqual(rejected, [])
        self.assertEqual(log, [{'seq': 1, 'source': 'seed.csv',
                                'received': 70, 'accepted': 70,
                                'ignored': 0, 'rejected': 0}])

    def test_bad_rows_listed_separately_and_never_mixed_in(self):
        incoming = series(60)
        bad_missing = row(5, '新闻一频')
        del bad_missing['reach']
        bad_bounds = row(6, '新闻一频', planned_min=0)
        incoming += [bad_missing, bad_bounds]
        valid, rejected, _ = initial_ingest(incoming, 'mixed.csv')
        self.assertEqual(len(valid), 60)
        self.assertEqual(len(rejected), 2)
        self.assertEqual([item['index'] for item in rejected], [61, 62])
        self.assertTrue(all('原因' in item['reason'] or item['reason'] for item in rejected))
        self.assertTrue(all(item['record'] not in valid for item in rejected))

    def test_same_key_duplicate_in_first_batch_is_rejected(self):
        incoming = series(60)
        incoming.append(dict(incoming[10]))  # 完全相同的同链路同日记录
        valid, rejected, _ = initial_ingest(incoming, 'dup.csv')
        self.assertEqual(len(valid), 60)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]['reason'], REASON_DUPLICATE)

    def test_same_key_conflict_in_first_batch_is_rejected(self):
        incoming = series(60)
        revised = dict(incoming[10])
        revised['interruption_min'] = 99.0
        revised['aired_min'] = revised['planned_min'] - 99.0
        incoming.append(revised)
        valid, rejected, _ = initial_ingest(incoming, 'conflict.csv')
        self.assertEqual(len(valid), 60)
        self.assertEqual(rejected[0]['reason'], REASON_CONFLICT)


class MergeIngestTests(unittest.TestCase):
    def setUp(self):
        self.records, self.rejected, self.log = initial_ingest(series(80), 'base.csv')

    def test_late_arriving_day_merged_by_channel_and_day(self):
        late = [row(95, '新闻一频'), row(96, '新闻一频')]
        merged, rejected, log, stats = merge_ingest(
            self.records, self.rejected, self.log, late, 'late.csv')
        self.assertEqual(stats['received'], 2)
        self.assertEqual(stats['accepted'], 2)
        self.assertEqual(stats['ignored'], 0)
        self.assertEqual(stats['rejected'], 0)
        self.assertEqual(len(merged), 82)
        self.assertEqual(log[-1]['seq'], 2)
        keys = {(r['channel'], r['day']) for r in merged}
        self.assertIn(('新闻一频', str(START + timedelta(days=96))), keys)

    def test_identical_resubmission_ignored_and_does_not_change_result(self):
        before = [dict(r) for r in self.records]
        dup = [dict(self.records[0]), dict(self.records[5])]
        merged, _, _, stats = merge_ingest(
            self.records, self.rejected, self.log, dup, 'dup.csv')
        self.assertEqual(stats['ignored'], 2)
        self.assertEqual(stats['accepted'], 0)
        self.assertEqual(merged, before)

    def test_same_day_newer_revision_wins(self):
        old = next(r for r in self.records if r['day'] == str(START + timedelta(days=10)))
        revised = dict(old)
        revised['interruption_min'] = 42.0
        revised['aired_min'] = round(old['planned_min'] - 42.0, 2)
        merged, _, _, stats = merge_ingest(
            self.records, self.rejected, self.log, [revised], 'correction.csv')
        self.assertEqual(stats['accepted'], 1)
        stored = next(r for r in merged if (r['channel'], r['day']) == (old['channel'], old['day']))
        self.assertEqual(stored['interruption_min'], 42.0)
        self.assertEqual(len(merged), len(self.records))

    def test_older_revision_cannot_overwrite_newer(self):
        # 先并入一个修订（批次2），再用声明“只针对第1版”的旧内容补报（批次3）：
        # 旧修订必须被忽略，不能回滚已生效的新修订，也不算坏记录。
        old = next(r for r in self.records if r['day'] == str(START + timedelta(days=10)))
        revised = dict(old)
        revised['interruption_min'] = 42.0
        revised['aired_min'] = round(old['planned_min'] - 42.0, 2)
        records2, rej2, log2, stats2 = merge_ingest(
            self.records, self.rejected, self.log, [revised], 'correction.csv')
        self.assertEqual(stats2['accepted'], 1)
        legacy = dict(old)
        legacy['replaces_revision'] = 1  # 声明它只针对第1版，当前已到第2版
        merged, rej3, log3, stats3 = merge_ingest(records2, rej2, log2, [legacy], 'old.csv')
        stored = next(r for r in merged if (r['channel'], r['day']) == (old['channel'], old['day']))
        self.assertEqual(stored['interruption_min'], 42.0)
        self.assertEqual(stats3['ignored'], 1)
        self.assertEqual(stats3['accepted'], 0)
        self.assertEqual(stats3['rejected'], 0)  # 合法旧修订不是坏记录
        self.assertEqual(log3[-1]['seq'], 3)
        # 留痕里能查到该旧修订被忽略的原因
        self.assertTrue(any(item.get('kind') == 'ignored' and item['reason'] == REASON_OLD
                            for item in rej3))
        self.assertNotIn('replaces_revision', stored)  # 内部控制字段不进入业务记录

    def test_bad_records_in_supplement_rejected_not_merged(self):
        bad = row(200, '新闻一频', planned_min=0)
        good = row(200, '文化二频')
        merged, rejected, _, stats = merge_ingest(
            self.records, self.rejected, self.log, [bad, good], 'mixed-late.csv')
        self.assertEqual(stats['received'], 2)
        self.assertEqual(stats['accepted'], 1)
        self.assertEqual(stats['rejected'], 1)
        self.assertEqual(len(merged), 81)
        self.assertEqual(rejected[-1]['batch_seq'], 2)
        self.assertIn('计划分钟', rejected[-1]['reason'])

    def test_conflict_within_one_supplement_rejected(self):
        a = row(30, '新闻一频', interruption=5.0)
        b = row(30, '新闻一频', interruption=9.0)
        merged, rejected, _, stats = merge_ingest(
            self.records, self.rejected, self.log, [a, b], 'conflict.csv')
        self.assertEqual(stats['rejected'], 1)
        self.assertEqual(rejected[-1]['reason'], REASON_CONFLICT)
        stored = next(r for r in merged if r['day'] == a['day'] and r['channel'] == a['channel'])
        self.assertEqual(stored['interruption_min'], 5.0)  # 先到的一版生效

    def test_multi_channel_merge_keeps_each_channel_independent(self):
        late = [row(120, c) for c in CHANNELS]
        merged, _, _, stats = merge_ingest(
            self.records, self.rejected, self.log, late, 'multi.csv')
        self.assertEqual(stats['accepted'], 3)
        per_channel = {c: [r for r in merged if r['channel'] == c] for c in CHANNELS}
        self.assertTrue(all(rows[-1]['day'] == str(START + timedelta(days=120))
                            for rows in per_channel.values()))


class SummaryTests(unittest.TestCase):
    def test_balance_identity_holds_across_batches(self):
        records, rejected, log = initial_ingest(series(60) + [row(0, '新闻一频', planned_min=0)], 'b1.csv')
        good_late = row(80, '新闻一频')
        dup = dict(records[0])
        records, rejected, log, _ = merge_ingest(records, rejected, log,
                                                 [good_late, dup, row(81, '新闻一频', planned_min=0)], 'b2.csv')
        summary = summarize(records, rejected, log)
        self.assertTrue(summary['balanced'])
        self.assertEqual(summary['received_total'], 61 + 3)
        self.assertEqual(summary['rejected_total'], 2)
        self.assertEqual(summary['ignored_total'], 1)
        self.assertEqual(summary['accepted_total'], 60 + 1)
        self.assertEqual(summary['current_rows'], 61)


# ---- 固定基准重算：更正历史日后，基准仍取最初三十日，后续异常判断重算 ----

def ewma_baseline(values, smoothing=0.2, limit=3.0):
    mean = statistics.mean(values[:30])
    sigma = statistics.stdev(values[:30])
    z, out = mean, []
    for index, value in enumerate(values[30:], 1):
        z = smoothing * value + (1 - smoothing) * z
        width = limit * sigma * math.sqrt(smoothing / (2 - smoothing) * (1 - (1 - smoothing) ** (2 * index)))
        out.append(z < mean - width or z > mean + width)
    return mean, sigma, out


class FixedBaselineRecomputeTests(unittest.TestCase):
    def _completion_series(self, interruptions, planned=720.0):
        return [(planned - x) / planned * 100 for x in interruptions]

    def test_correction_inside_baseline_changes_judgement_but_keeps_baseline_window(self):
        # 构造 80 日序列，第 40 日起注入持续偏移
        interruptions = [3.0 + 0.05 * i for i in range(80)]
        for i in range(40, 55):
            interruptions[i] += 12.0
        values = self._completion_series(interruptions)
        mean_before, sigma_before, flags_before = ewma_baseline(values)

        # 更正基准期内第 10 日（历史日），只改这一天
        corrected = list(interruptions)
        corrected[9] += 6.0
        values2 = self._completion_series(corrected)
        mean_after, sigma_after, flags_after = ewma_baseline(values2)

        # 基准窗口仍是最初30日（监测长度不变），但数值随更正重算
        self.assertEqual(len(flags_before), len(flags_after))
        self.assertNotAlmostEqual(mean_before, mean_after)
        self.assertNotAlmostEqual(sigma_before, sigma_after)
        # 基准 σ 变大后控制限变宽，越限数量只减不增，证明后续异常判断被重算
        self.assertLessEqual(sum(flags_after), sum(flags_before))
        # 基准均值/标准差恰好只由前30日决定
        self.assertAlmostEqual(mean_after, statistics.mean(values2[:30]))
        self.assertAlmostEqual(sigma_after, statistics.stdev(values2[:30]))

    def test_baseline_never_uses_future_values(self):
        # 无论怎么更正第 31 日之后的数据，前30日 μ/σ 必须完全不变
        interruptions = [3.0 + 0.05 * i for i in range(80)]
        values = self._completion_series(interruptions)
        mean0, sigma0, _ = ewma_baseline(values)
        for idx in range(30, 80):
            tweaked = list(interruptions)
            tweaked[idx] += 30.0
            mean1, sigma1, _ = ewma_baseline(self._completion_series(tweaked))
            self.assertAlmostEqual(mean0, mean1)
            self.assertAlmostEqual(sigma0, sigma1)

    def test_correction_in_monitoring_window_recomputes_downstream_anomalies(self):
        # 监测期内的更正沿 EWMA 递推改变其后所有判断；基准 μ/σ 固定不变。
        # 直接构造数值序列：基准30日围绕100有确定波动，监测期50日全部为100（受控）。
        baseline = [100.0, 99.9] * 15
        values = baseline + [100.0] * 50
        mean0, sigma0, flags0 = ewma_baseline(values)
        self.assertEqual(sum(flags0), 0)  # 原始监测期全部受控
        # 更正监测期首日为 105：EWMA 被持续拉高，随后出现越限
        tweaked = list(values)
        tweaked[30] = 105.0
        mean1, sigma1, flags1 = ewma_baseline(tweaked)
        # 基准仍只由最初30日决定，完全不变
        self.assertAlmostEqual(mean0, mean1)
        self.assertAlmostEqual(sigma0, sigma1)
        # 后续越限判断被重算：由零越限变为存在越限
        self.assertNotEqual(flags0, flags1)
        self.assertGreater(sum(flags1), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
