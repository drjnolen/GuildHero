import datetime
import unittest
from CityLedger.scoring import contribution_messages, contribution_total, score_transcript, validate_scores


def message(text, day=1, reply=False):
    return {'text': text, 'date': f'2026-09-{day:02d}T12:00:00+00:00', 'is_reply': reply}


class ScoringTests(unittest.TestCase):
    def test_strict_ranges_and_keys(self):
        good = dict(quality=10, tone=18, helpfulness=10, humor=0)
        self.assertEqual(validate_scores(good)['tone'], 18)
        for change in [dict(tone=60), dict(tone=-1), dict(tone=True), dict(tone='10'),
                       dict(tone=float('inf')), dict(tone=float('nan')), dict(total=100)]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_scores({**good, **change})
        with self.assertRaises(ValueError):
            validate_scores({'tone': 10})

    def test_duplicate_flood_and_replies_do_not_boost_activity(self):
        original = message('Useful explanation about wallet recovery')
        duplicates = [original] + [message('USEFUL explanation about wallet recovery!!!', 2, True)] * 1000
        unique, count, days = contribution_messages(duplicates)
        self.assertEqual((len(unique), count, days), (1, 1, 1))
        self.assertEqual(contribution_messages([message('gm'), message('👍'), message('yes', reply=True)])[1:], (0, 0))

    def test_consistency_beats_same_day_burst_at_equal_quality(self):
        metrics = dict(quality=16, tone=14, helpfulness=16, humor=0)
        self.assertGreater(contribution_total(metrics, 70, 7), contribution_total(metrics, 10, 1))
        self.assertGreater(contribution_total(metrics, 10, 3),
                           contribution_total(dict(quality=4, tone=20, helpfulness=2, humor=20), 100, 7))
        self.assertEqual(contribution_total(dict.fromkeys(metrics, 20), 10000, 100), 100)

    def test_humor_adds_only_about_one_point_at_max_activity(self):
        metrics = dict(quality=12, tone=12, helpfulness=12, humor=0)
        without_humor = contribution_total(metrics, 100, 7)
        with_humor = contribution_total({**metrics, 'humor': 20}, 100, 7)
        # Previously the full humor range added 5 points; now it adds ~1.04.
        self.assertAlmostEqual(with_humor - without_humor, 1.04, places=2)

    def test_activity_beats_humor_at_equal_other_categories(self):
        metrics = dict(quality=12, tone=12, helpfulness=11, humor=0)
        active = contribution_total(metrics, 10, 1)
        funny = contribution_total({**metrics, 'humor': 8}, 5, 1)
        self.assertGreater(active, funny)

    def test_total_scale_is_preserved_with_lower_humor_weight(self):
        metrics = dict(quality=20, tone=20, helpfulness=20, humor=0)
        self.assertEqual(contribution_total(dict.fromkeys(metrics, 0), 100, 7), 0)
        self.assertEqual(contribution_total(metrics, 100, 7), 98.96)
        self.assertEqual(contribution_total({**metrics, 'humor': 20}, 100, 7), 100)

    def test_daily_activity_is_capped(self):
        messages = [message(f'Useful explanation of distinct issue number {i}') for i in range(50)]
        self.assertEqual(contribution_messages(messages)[1:], (10, 1))

    def test_transcript_covers_early_and_late_messages_under_limit(self):
        messages = [message(f'entry{i} ' + 'x' * 10000) for i in range(30)]
        transcript = score_transcript(messages, 10, 1200)
        self.assertLessEqual(len(transcript), 1200)
        self.assertIn('entry0 ', transcript)
        self.assertIn('entry29 ', transcript)

    def test_reply_marker_alone_does_not_change_total(self):
        a = contribution_messages([message('A detailed useful answer here', reply=False)])
        b = contribution_messages([message('A detailed useful answer here', reply=True)])
        self.assertEqual(a[1:], b[1:])
