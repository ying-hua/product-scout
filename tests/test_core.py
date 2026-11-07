import unittest
from datetime import datetime, timedelta, timezone

from product_scout.coverage import Video, assess
from product_scout.db import DB, Item, iso
from product_scout.keys import canonical_url, product_key
from product_scout.llm import extract_json, provider_for
from product_scout.rank import is_window, momentum_score, priority, velocity
from product_scout.scoring import combine, normalize, score_with, weighted
from product_scout.sources.hackernews import is_repo_root, product_name

UTC = timezone.utc
WEIGHTS = {"visual": 0.3, "hands_on": 0.2, "paid_alternative": 0.15, "audience_fit": 0.25, "novelty": 0.1}


def item(key="github:a/b", source="github", ext="repo:1", metric=100.0, **kw):
    return Item(source=source, external_id=ext, key=key, name=kw.pop("name", "b"), url=canonical_url(key),
                mention_url=canonical_url(key), metric_name="stars", metric_value=metric, **kw)


class KeysTest(unittest.TestCase):
    def test_github_variants_merge(self):
        for url in ["https://github.com/Foo/Bar", "https://www.github.com/foo/bar.git",
                    "https://github.com/foo/bar/tree/main/src", "http://github.com/foo/bar#readme"]:
            self.assertEqual(product_key(url), "github:foo/bar")

    def test_other_hosts(self):
        self.assertEqual(product_key("https://huggingface.co/spaces/A/B"), "hf-space:a/b")
        self.assertEqual(product_key("https://www.uncracked.city/play"), "site:uncracked.city")
        self.assertEqual(product_key("https://user.github.io/proj/x"), "github-pages:user/proj")
        self.assertTrue(product_key("https://x.com/someone/status/1").startswith("url:x.com/"))
        self.assertIsNone(product_key("https://github.com/trending"))
        self.assertIsNone(product_key(""))

    def test_canonical_roundtrip(self):
        self.assertEqual(product_key(canonical_url("github:foo/bar")), "github:foo/bar")
        self.assertEqual(product_key(canonical_url("hf-space:a/b")), "hf-space:a/b")


class LLMTest(unittest.TestCase):
    def test_provider_detection(self):
        self.assertEqual(provider_for({"base_url": "https://api.anthropic.com/v1"}), "anthropic")
        self.assertEqual(provider_for({"base_url": "https://api.deepseek.com/v1"}), "openai")
        self.assertEqual(provider_for({"provider": "anthropic", "base_url": ""}), "anthropic")

    def test_extract_json(self):
        self.assertEqual(extract_json('好的：```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('前言 {"a": {"b": 2}} 结尾'), {"a": {"b": 2}})


class HackerNewsTest(unittest.TestCase):
    def test_product_name(self):
        self.assertEqual(product_name("Show HN: Subvocal – Talk to your computer"), "Subvocal")
        self.assertEqual(product_name("Show HN: Gauge - Open-source CLI"), "Gauge")
        self.assertEqual(product_name("Show HN: I built Foo, a thing"), "Foo")

    def test_repo_root(self):
        self.assertTrue(is_repo_root("https://github.com/a/b"))
        self.assertTrue(is_repo_root("https://github.com/a/b/"))
        self.assertFalse(is_repo_root("https://github.com/a/b/issues/3"))
        self.assertFalse(is_repo_root("https://example.com/a/b"))


class DBTest(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")

    def test_same_product_from_two_sources_merges(self):
        pid1, new1 = self.db.upsert_item(item())
        pid2, new2 = self.db.upsert_item(item(source="hackernews", ext="item:9", metric=300.0))
        self.assertEqual(pid1, pid2)
        self.assertTrue(new1)
        self.assertFalse(new2)
        self.assertEqual(len(self.db.mentions(pid1)), 2)

    def test_snapshots_throttled_to_one_per_hour(self):
        t = datetime(2026, 11, 1, tzinfo=UTC)
        pid, _ = self.db.upsert_item(item(metric=100), at=t)
        self.db.upsert_item(item(metric=150), at=t + timedelta(minutes=20))
        self.db.upsert_item(item(metric=300), at=t + timedelta(hours=2))
        values = [s["value"] for s in self.db.snapshots(pid, "stars")]
        self.assertEqual(values, [150, 300])

    def test_status_change_logged(self):
        pid, _ = self.db.upsert_item(item())
        self.db.set_status(pid, "watching")
        self.assertEqual(self.db.product(pid)["status"], "watching")
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM status_log").fetchone()[0], 1)
        with self.assertRaises(ValueError):
            self.db.set_status(pid, "bogus")


class RankTest(unittest.TestCase):
    def test_velocity_uses_readings_a_day_apart(self):
        t = datetime(2026, 11, 5, tzinfo=UTC)
        pts = [(t - timedelta(days=2), 1000.0), (t - timedelta(days=1), 2000.0), (t, 3000.0)]
        self.assertAlmostEqual(velocity(pts, None, t), 1000.0)

    def test_velocity_falls_back_to_average_since_creation(self):
        t = datetime(2026, 11, 5, tzinfo=UTC)
        self.assertAlmostEqual(velocity([(t, 7000.0)], t - timedelta(days=7), t), 1000.0)

    def test_momentum_is_bounded(self):
        self.assertEqual(momentum_score(0, 1500), 0)
        self.assertEqual(momentum_score(10 ** 6, 1500), 1.0)

    def test_priority_keeps_evergreen_items(self):
        self.assertGreater(priority(90, 0.0), priority(30, 1.0))
        self.assertEqual(priority(None, 0.0), 17.5)  # unscored uses a neutral default

    def test_window_rules(self):
        row = {"coverage": None, "status": "new"}
        cfg = {"window_min_value": 70, "window_min_timeliness": 0.6}
        self.assertTrue(is_window(row, 80, 0.7, cfg))
        self.assertFalse(is_window(row, None, 0.9, cfg))
        self.assertFalse(is_window({**row, "coverage": "heavy"}, 80, 0.7, cfg))
        self.assertFalse(is_window({**row, "status": "published"}, 80, 0.7, cfg))


class ScoringTest(unittest.TestCase):
    def raw(self, v, **kw):
        return {"relevant": True, "dimensions": {d: v for d in WEIGHTS}, "risks": [], "pitch": "p", **kw}

    def test_normalize_clamps_and_filters(self):
        n = normalize({"dimensions": {"visual": 14, "hands_on": "x"}, "risks": ["NSFW", "made-up"], "relevant": "false"})
        self.assertEqual(n["dimensions"]["visual"], 10)
        self.assertEqual(n["dimensions"]["hands_on"], 0)
        self.assertEqual(n["risks"], ["nsfw"])
        self.assertFalse(n["relevant"])

    def test_weighted_full_marks_is_100(self):
        self.assertAlmostEqual(weighted({d: 10 for d in WEIGHTS}, WEIGHTS), 100)

    def test_third_opinion_only_when_runs_disagree(self):
        calls = iter([self.raw(8), self.raw(8)])
        r = score_with(lambda: next(calls), WEIGHTS, {})
        self.assertEqual(len(r["detail"]["runs"]), 2)
        calls = iter([self.raw(9), self.raw(3), self.raw(7)])
        r = score_with(lambda: next(calls), WEIGHTS, {})
        self.assertEqual(len(r["detail"]["runs"]), 3)
        self.assertAlmostEqual(r["value"], 70)  # median of 9, 3, 7

    def test_risk_penalty_and_irrelevance_cap(self):
        r = combine([normalize(self.raw(10, risks=["grey"]))], WEIGHTS, {"grey": 0.3})
        self.assertAlmostEqual(r["value"], 30)
        r = combine([normalize(self.raw(10, relevant=False))], WEIGHTS, {})
        self.assertLessEqual(r["value"], 20)


class CoverageTest(unittest.TestCase):
    def test_assess_matches_name_and_date(self):
        t = datetime(2026, 11, 1, tzinfo=UTC)
        videos = [
            Video("Craft Anything 实测", "u1", t + timedelta(days=1), 15000),
            Video("craft-anything 复刻一切", "u2", t + timedelta(days=2), 8000),
            Video("Minecraft anything goes", "u3", t + timedelta(days=1), 99999),  # name not in title
            Video("Craft Anything 老视频", "u4", t - timedelta(days=30), 5000),  # before the product existed
        ]
        r = assess(videos, ["Craft Anything"], t)
        self.assertEqual(r["videos"], 2)
        self.assertEqual(r["level"], "heavy")  # 23k plays
        self.assertEqual(assess([], ["x-tool"], t)["level"], "none")
        self.assertEqual(assess([Video("craftanything", "u5", t, 10)], ["Craft Anything"], t)["level"], "light")


if __name__ == "__main__":
    unittest.main()
