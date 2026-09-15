#!/usr/bin/env python3
"""Tests for the local re-rating pass. Stdlib + diskcache, no network."""
from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from diskcache import Cache  # type: ignore

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ai_marketplace_monitor.ai import AIResponse  # noqa: E402
from ai_marketplace_monitor.listing import Listing  # noqa: E402
from ai_marketplace_monitor.rescore import SCRIPT_VERDICT_NAME, rescore  # noqa: E402
from ai_marketplace_monitor.utils import CacheType  # noqa: E402


class RescoreTest(unittest.TestCase):
    """A notified listing, its details, and whatever verdict it carries."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Cache(self.tmp.name)
        self.addCleanup(self.cache.close)

    def seed(self, title="Samsung Galaxy S21 5G 128GB", price="€100",
             verdict_name="groq", score=5):
        listing = Listing(
            marketplace="facebook", name="smartphones", id="123", title=title,
            image="", price=price,
            post_url="https://www.facebook.com/marketplace/item/123/",
            location="Bratislava", seller="Someone", condition="Used - Good",
            description="",
        )
        listing.to_cache(listing.post_url, self.cache)
        self.cache.set(
            (CacheType.USER_NOTIFIED.value, "facebook", "123", "me"),
            ("2026-09-01 10:00:00", listing.hash, price),
            tag=CacheType.USER_NOTIFIED.value,
        )
        if verdict_name:
            self.cache.set(
                (CacheType.AI_INQUIRY.value, "i", "m", listing.hash),
                asdict(AIResponse(score=score, comment="a model said so",
                                  name=verdict_name)),
                tag=CacheType.AI_INQUIRY.value,
            )
        return listing

    def stored(self, listing):
        for k in self.cache.iterkeys():
            if (isinstance(k, tuple) and len(k) >= 4
                    and k[0] == CacheType.AI_INQUIRY.value and k[3] == listing.hash):
                return self.cache.get(k)
        return None

    def test_dry_run_changes_nothing(self):
        listing = self.seed()
        with mock.patch("ai_marketplace_monitor.rescore.deterministic_verdict",
                        return_value={"decision": "deal", "score": 5,
                                      "comment": "computed", "facts": {}}):
            stats = rescore(apply=False, verbose=False, local_cache=self.cache)
        self.assertEqual(stats["rescored"], 1)
        self.assertEqual(self.stored(listing)["name"], "groq",
                         "a dry run must not touch the stored verdict")

    def test_apply_replaces_the_model_verdict(self):
        listing = self.seed()
        with mock.patch("ai_marketplace_monitor.rescore.deterministic_verdict",
                        return_value={"decision": "reject", "score": 1,
                                      "comment": "computed", "facts": {}}):
            rescore(apply=True, verbose=False, local_cache=self.cache)
        got = self.stored(listing)
        self.assertEqual(got["name"], SCRIPT_VERDICT_NAME)
        self.assertEqual(got["score"], 1)
        self.assertEqual(got["comment"], "computed")

    def test_only_one_verdict_survives(self):
        """The row join takes whichever entry it meets first, so a stale one
        left behind can win and the re-rating appears not to have happened."""
        listing = self.seed()
        with mock.patch("ai_marketplace_monitor.rescore.deterministic_verdict",
                        return_value={"decision": "deal", "score": 5,
                                      "comment": "computed", "facts": {}}):
            rescore(apply=True, verbose=False, local_cache=self.cache)
        kept = [k for k in self.cache.iterkeys()
                if isinstance(k, tuple) and len(k) >= 4
                and k[0] == CacheType.AI_INQUIRY.value and k[3] == listing.hash]
        self.assertEqual(len(kept), 1)

    def test_listing_the_tables_cannot_settle_is_left_alone(self):
        listing = self.seed()
        with mock.patch("ai_marketplace_monitor.rescore.deterministic_verdict",
                        return_value=None):
            stats = rescore(apply=True, verbose=False, local_cache=self.cache)
        self.assertEqual(stats["needs_model"], 1)
        self.assertEqual(stats["rescored"], 0)
        self.assertEqual(self.stored(listing)["name"], "groq")

    def test_an_already_current_verdict_is_not_rewritten(self):
        listing = self.seed(verdict_name=SCRIPT_VERDICT_NAME, score=1)
        self.cache.set(
            (CacheType.AI_INQUIRY.value, "i", "m", listing.hash),
            asdict(AIResponse(score=1, comment="computed",
                              name=SCRIPT_VERDICT_NAME)),
            tag=CacheType.AI_INQUIRY.value,
        )
        with mock.patch("ai_marketplace_monitor.rescore.deterministic_verdict",
                        return_value={"decision": "reject", "score": 1,
                                      "comment": "computed", "facts": {}}):
            stats = rescore(apply=True, verbose=False, local_cache=self.cache)
        self.assertEqual(stats["unchanged"], 1)
        self.assertEqual(stats["rescored"], 0)

    def test_never_calls_a_model(self):
        """The whole point: local data only, so it costs no quota."""
        self.seed()
        import ai_marketplace_monitor.device_facts as df
        with mock.patch.object(df, "fetch_specs",
                               side_effect=AssertionError("network!")):
            rescore(apply=True, verbose=False, local_cache=self.cache)


if __name__ == "__main__":
    unittest.main(verbosity=1)
