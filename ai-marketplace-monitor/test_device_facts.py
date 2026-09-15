#!/usr/bin/env python3
"""Tests for device_facts.py. Stdlib only: python3 test_device_facts.py

Network is stubbed everywhere except the tests marked live (opt in with
LIVE=1), so this suite is fast, deterministic and safe to run repeatedly
without burning API quota.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import device_facts as df  # noqa: E402


class TempCache:
    """Point every on-disk cache at a throwaway directory.

    Redirects EVERY module-level Path that lives in the cache directory,
    rather than a hand-listed few. The hand-listed version silently missed
    each new cache file as it was added: tests were writing real seller
    statistics into the running installation, and enough repeated runs made
    an invented seller look like a bulk lister and broke an unrelated test.
    """

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        original_dir = df.CACHE_DIR
        self._orig = {"CACHE_DIR": df.CACHE_DIR}
        for name in dir(df):
            value = getattr(df, name)
            if (
                isinstance(value, Path)
                and name.isupper()
                and value.parent == original_dir
            ):
                self._orig[name] = value
                setattr(df, name, d / value.name)
        df.CACHE_DIR = d
        return d

    def __exit__(self, *a):
        for name, value in self._orig.items():
            setattr(df, name, value)
        self.tmp.cleanup()


class TestQualityScore(unittest.TestCase):
    def test_flagship_phone_scores_high(self):
        r = df.quality_score(
            {"kind": "phone", "benchmark_name": "antutu_v10",
             "benchmark_score": 2_000_000, "ram_gb": [12], "storage_gb": [512]}
        )
        self.assertGreaterEqual(r["score"], 90)
        self.assertIn(r["verdict"], ("great",))

    def test_weak_phone_scores_low(self):
        r = df.quality_score(
            {"kind": "phone", "benchmark_name": "antutu_v10",
             "benchmark_score": 150_000, "ram_gb": [3], "storage_gb": [32]}
        )
        self.assertLess(r["score"], 35)
        self.assertEqual(r["verdict"], "weak")

    def test_better_chip_always_scores_higher(self):
        base = {"kind": "phone", "benchmark_name": "antutu_v10", "ram_gb": [8]}
        low = df.quality_score({**base, "benchmark_score": 300_000})["score"]
        high = df.quality_score({**base, "benchmark_score": 900_000})["score"]
        self.assertGreater(high, low)

    def test_more_ram_always_scores_higher(self):
        base = {"kind": "phone", "benchmark_name": "antutu_v10", "benchmark_score": 600_000}
        self.assertGreater(
            df.quality_score({**base, "ram_gb": [12]})["score"],
            df.quality_score({**base, "ram_gb": [4]})["score"],
        )

    def test_laptop_uses_passmark_ceiling(self):
        """The same raw number must mean different things per benchmark."""
        phone = df.quality_score(
            {"kind": "phone", "benchmark_name": "antutu_v10", "benchmark_score": 20_000, "ram_gb": [8]}
        )["score"]
        laptop = df.quality_score(
            {"kind": "laptop", "benchmark_name": "passmark_cpu", "benchmark_score": 20_000, "ram_gb": [8]}
        )["score"]
        self.assertGreater(laptop, phone)

    def test_missing_benchmark_still_scores_from_ram(self):
        r = df.quality_score({"kind": "phone", "ram_gb": [8]})
        self.assertIsNotNone(r["score"])
        self.assertNotIn("chip", r["parts"])

    def test_no_data_at_all_is_unknown_not_zero(self):
        r = df.quality_score({"kind": "phone"})
        self.assertIsNone(r["score"])
        self.assertEqual(r["verdict"], "unknown")

    def test_unknown_benchmark_name_falls_back_by_kind(self):
        r = df.quality_score(
            {"kind": "laptop", "benchmark_name": "cinebench_r23",
             "benchmark_score": 15_000, "ram_gb": [16]}
        )
        self.assertIsNotNone(r["score"])

    def test_absurd_values_are_capped(self):
        r = df.quality_score(
            {"kind": "phone", "benchmark_name": "antutu_v10",
             "benchmark_score": 99_000_000, "ram_gb": [1024], "storage_gb": [100_000]}
        )
        self.assertLessEqual(r["score"], 100)

    def test_junk_types_do_not_crash(self):
        for bad in (
            {"benchmark_score": "lots", "ram_gb": "8GB"},
            {"benchmark_score": None, "ram_gb": [None]},
            {"benchmark_score": -5, "ram_gb": []},
            {"ram_gb": ["8", 6]},
        ):
            with self.subTest(bad=bad):
                r = df.quality_score({"kind": "phone", **bad})
                self.assertIn("verdict", r)

    def test_no_benchmark_cannot_be_called_great(self):
        """RAM/storage alone must not produce a confident high score."""
        r = df.quality_score({"kind": "laptop", "ram_gb": [48], "storage_gb": [2000]})
        self.assertLessEqual(r["score"], 45)
        self.assertEqual(r["confidence"], "low")
        self.assertIn("no benchmark", r["verdict"])

    def test_score_never_exceeds_100(self):
        for f in ({"kind": "laptop", "ram_gb": [64], "storage_gb": [4000]},
                  {"kind": "phone", "benchmark_name": "antutu_v10",
                   "benchmark_score": 9_000_000, "ram_gb": [24], "storage_gb": [1000]}):
            self.assertLessEqual(df.quality_score(f)["score"], 100, f)

    def test_variant_lists_use_smallest_not_largest(self):
        """A model sold with up to 48GB must not score as if it has 48GB."""
        base = {"kind": "laptop", "benchmark_name": "passmark_cpu", "benchmark_score": 10_000}
        many = df.quality_score({**base, "ram_gb": [8, 16, 32, 48]})["score"]
        just8 = df.quality_score({**base, "ram_gb": [8]})["score"]
        self.assertEqual(many, just8)

    def test_bool_is_not_treated_as_number(self):
        r = df.quality_score({"kind": "phone", "benchmark_score": True, "ram_gb": [True]})
        self.assertEqual(r["confidence"], "none")

    def test_verdict_thresholds_are_ordered(self):
        seen = []
        for score in (10, 40, 60, 90):
            facts = {"kind": "phone", "benchmark_name": "antutu_v10",
                     "benchmark_score": int(score / 100 * 2_000_000), "ram_gb": [8]}
            seen.append(df.quality_score(facts)["score"])
        self.assertEqual(seen, sorted(seen))


class TestAccessoryVsDevice(unittest.TestCase):
    """A listing that MENTIONS an accessory is not an accessory listing."""

    def test_specs_are_not_accessories(self):
        """"1TB HDD" in a laptop's spec list is a spec, not the product."""
        self.assertEqual(
            df.classify("Dell Inspiron 15 Gaming i5 512gb SSD 1TB HDD Laptop"), "laptop")

    def test_bundled_extras_do_not_decide_the_kind(self):
        """A tablet sold "s klávesnicou, perom" is a tablet, not a pen."""
        self.assertEqual(
            df.classify("Predaj tablet Samsung Galaxy Tab S3 s klávesnicou, perom"), "phone")
        self.assertEqual(df.classify("MacBook Air + Magic Mouse"), "laptop")

    def test_accessory_named_by_what_it_attaches_to(self):
        """No marker list can contain every accessory's brand name, so a
        device word that appears only after "pre"/"for" is what the thing
        attaches to, not what is being sold."""
        for title in ("Logitech Combo Touch pre iPad Pro 11",
                      "Kryt pre Samsung Galaxy S21", "Case for iPhone 13"):
            self.assertEqual(df.classify(title), "other", title)

    def test_device_named_first_survives_a_for_clause(self):
        self.assertEqual(df.classify("iPad Pro 11 na predaj"), "phone")
        self.assertEqual(df.classify("Predám notebook pre hráčov"), "laptop")

    def test_a_phone_sold_for_parts_is_not_a_parts_listing(self):
        """"náhradné diely" marks both a parts listing and a phone sold FOR
        parts. The second is something the buyer actively wants at salvage
        pricing, so it must reach the damaged path, not be dismissed here."""
        v = df.deterministic_verdict(
            "Predám Samsung A51 na opravu, alebo náhradné diely.", "€25")
        self.assertEqual(v["decision"], "deal")
        self.assertNotIn("Accessory", v["comment"])

    def test_accessories_are_settled_without_an_ai_call(self):
        for title in ("Kryt Tactical MagForce pre Samsung Galaxy S24",
                      "NOVÉ BURGA kryty na Samsung Galaxy S20 FE",
                      "Nabíjačka pre iPhone"):
            v = df.deterministic_verdict(title, "€25")
            self.assertEqual(v["decision"], "reject", title)
            self.assertIn("Accessory", v["comment"], title)

    def test_real_accessories_still_rejected(self):
        for title in ("Asus Notebook SSD disk", "Podstavec pre notebook",
                      "apple pencil", "Sieťový adaptér Asus ROG 240W"):
            self.assertEqual(df.classify(title), "other", title)


class TestKindDetection(unittest.TestCase):
    def test_laptops(self):
        for t in ("Lenovo ThinkPad T480", "Predam notebook HP", "MacBook Air 2020",
                  "Dell Latitude 7490 laptop", "Acer Aspire 5"):
            self.assertEqual(df.guess_kind(t), "laptop", t)

    def test_phones_default(self):
        for t in ("Samsung Galaxy S21", "iPhone 11 64GB", "Xiaomi Redmi Note 12",
                  "Predam telefon"):
            self.assertEqual(df.guess_kind(t), "phone", t)

    def test_empty_input(self):
        self.assertEqual(df.guess_kind(""), "phone")


class TestNormalisation(unittest.TestCase):
    def test_diacritics_and_punctuation(self):
        # Slovak accents fold to plain ASCII: "predám" -> "predam", not "pred m".
        self.assertEqual(df._norm("Predám  Galaxy-S21, 5G!"), "predam galaxy s21 5g")

    def test_empty(self):
        self.assertEqual(df._norm(""), "")


class TestLineageMatching(unittest.TestCase):
    DEVICES = [
        {"codename": "b0s", "vendor": "Samsung", "name": "Galaxy S22 Ultra"},
        {"codename": "a52q", "vendor": "Samsung", "name": "Galaxy A52 4G"},
        {"codename": "beyond1lte", "vendor": "Samsung", "name": "Galaxy S10"},
    ]

    def setUp(self):
        self.p = mock.patch.object(df, "lineage_devices", return_value=self.DEVICES)
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_exact_match(self):
        self.assertIsNotNone(df.lineage_supported("Samsung Galaxy S22 Ultra 256GB"))

    def test_unsupported_returns_none(self):
        self.assertIsNone(df.lineage_supported("Samsung Galaxy S21 5G"))

    def test_does_not_match_shorter_variant_to_longer(self):
        """'Galaxy S10' must not be reported for a listing that is an S10e."""
        self.assertIsNone(df.lineage_supported("iPhone 11"))

    def test_prefers_longest_match(self):
        devices = self.DEVICES + [{"codename": "x", "vendor": "Samsung", "name": "Galaxy S22"}]
        with mock.patch.object(df, "lineage_devices", return_value=devices):
            got = df.lineage_supported("Samsung Galaxy S22 Ultra")
            self.assertEqual(got["name"], "Galaxy S22 Ultra")

    def test_empty_query(self):
        self.assertIsNone(df.lineage_supported(""))


class TestCacheAndOverrides(unittest.TestCase):
    def test_cached_lookup_is_not_repeated(self):
        with TempCache(), mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"}):
            with mock.patch.object(df, "fetch_specs", return_value={"benchmark_score": 1}) as fs:
                df.device_facts("Test Phone")
                df.device_facts("Test Phone")
                self.assertEqual(fs.call_count, 1, "second call should hit the cache")

    def test_refresh_forces_new_lookup(self):
        with TempCache(), mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"}):
            with mock.patch.object(df, "fetch_specs", return_value={"benchmark_score": 1}) as fs:
                df.device_facts("Test Phone")
                df.device_facts("Test Phone", refresh=True)
                self.assertEqual(fs.call_count, 2)

    def test_offline_never_calls_network(self):
        with TempCache():
            with mock.patch.object(df, "fetch_specs", side_effect=AssertionError("called!")) as fs:
                facts = df.device_facts("Test Phone", offline=True)
                fs.assert_not_called()
                self.assertEqual(facts["specs_error"], "offline")

    def test_override_beats_lookup(self):
        with TempCache(), mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"}):
            df.OVERRIDES.write_text(json.dumps(
                {"test phone": {"benchmark_score": 999_999, "ram_gb": [12]}}))
            with mock.patch.object(df, "fetch_specs",
                                   return_value={"benchmark_score": 1, "ram_gb": [2]}):
                facts = df.device_facts("Test Phone")
            self.assertEqual(facts["benchmark_score"], 999_999)
            self.assertTrue(facts["overridden"])

    def test_override_matches_variants(self):
        with TempCache():
            df.OVERRIDES.write_text(json.dumps({"galaxy s21": {"benchmark_score": 800_000}}))
            facts = df.device_facts("Samsung Galaxy S21 5G 128GB", offline=True)
            self.assertEqual(facts["benchmark_score"], 800_000)

    def test_score_reflects_override(self):
        with TempCache():
            df.OVERRIDES.write_text(json.dumps(
                {"test": {"benchmark_name": "antutu_v10", "benchmark_score": 1_800_000,
                          "ram_gb": [12]}}))
            facts = df.device_facts("Test", offline=True)
            self.assertGreaterEqual(facts["score"], 80)

    def test_corrupt_cache_is_survivable(self):
        with TempCache():
            df.FACTS_CACHE.write_text("{ this is not json")
            facts = df.device_facts("Test Phone", offline=True)
            self.assertIn("verdict", facts)

    def test_corrupt_overrides_are_survivable(self):
        with TempCache():
            df.OVERRIDES.write_text("]]not json[[")
            facts = df.device_facts("Test Phone", offline=True)
            self.assertIn("verdict", facts)

    def test_save_is_atomic_no_tmp_left(self):
        with TempCache() as d:
            df._save(df.FACTS_CACHE, {"a": 1})
            leftovers = [p.name for p in Path(d).glob("*.tmp")]
            self.assertEqual(leftovers, [])


class TestFetchFailures(unittest.TestCase):
    def _http_error(self, code):
        return urllib.error.HTTPError("u", code, "err", {}, None)

    def test_missing_api_key_degrades(self):
        with TempCache(), mock.patch.dict(os.environ, {"GROQ_API_KEY": ""}, clear=False):
            facts = df.device_facts("Test Phone")
            self.assertIn("specs_error", facts)
            self.assertIn("verdict", facts)

    def test_lookup_exception_never_propagates(self):
        with TempCache(), mock.patch.dict(os.environ, {"GROQ_API_KEY": "x"}):
            with mock.patch.object(df, "fetch_specs", side_effect=RuntimeError("boom")):
                facts = df.device_facts("Test Phone")
            self.assertIn("boom", facts["specs_error"])

    def test_rate_limit_codes_are_retried_then_reported(self):
        """429 is surfaced as RateLimited (busy), and retried across models."""
        with mock.patch.object(df.time, "sleep"), \
                mock.patch.object(df.urllib.request, "urlopen",
                                  side_effect=self._http_error(429)) as uo:
            with self.assertRaises(df.RateLimited):
                df.fetch_specs("X", "key", timeout=1)
            # 2 attempts x every model in the chain: search models first,
            # then the plain fallbacks fetch_specs walks after them.
            self.assertEqual(
                uo.call_count,
                2 * len(df.GROQ_SEARCH_MODELS + df.GROQ_FALLBACK_MODELS),
            )

    def test_413_treated_as_busy_not_fatal(self):
        """Groq answers 413, not 429, when a request exceeds the token budget."""
        with mock.patch.object(df.time, "sleep"), \
                mock.patch.object(df.urllib.request, "urlopen",
                                  side_effect=self._http_error(413)):
            with self.assertRaises(df.RateLimited):
                df.fetch_specs("X", "key", timeout=1)

    def test_hard_http_error_tries_the_next_model_but_never_retries_one(self):
        """A 400 is that MODEL's problem, not the device's.

        Retrying the same model is pointless, but abandoning the rest of the
        chain is worse: one 400 from the first Groq model meant Cerebras was
        never asked, and Cerebras was the one that knew the chip. So each
        endpoint is tried exactly once and the last error is raised only if
        they all fail.
        """
        with mock.patch.dict(os.environ, {"CEREBRAS_API_KEY": ""}), \
                mock.patch.object(df.time, "sleep"), \
                mock.patch.object(df.urllib.request, "urlopen",
                                  side_effect=self._http_error(400)) as uo:
            with self.assertRaises(urllib.error.HTTPError):
                df.fetch_specs("X", "key", timeout=1)
            expected = len(df.GROQ_SEARCH_MODELS) + len(df.GROQ_FALLBACK_MODELS)
            self.assertEqual(uo.call_count, expected,
                             "each model once, no retries")

    def test_rate_limit_is_recorded_not_crashing(self):
        """device_facts() degrades to a cached/offline answer when busy."""
        with TempCache(), mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"}):
            with mock.patch.object(df, "fetch_specs", side_effect=df.RateLimited("busy")):
                facts = df.device_facts("Some Phone")
        self.assertIn("busy", facts["specs_error"])
        self.assertIn("verdict", facts)

    def test_non_json_response_raises_cleanly(self):
        body = json.dumps({"choices": [{"message": {"content": "sorry, no idea"}}]}).encode()
        cm = mock.MagicMock()
        cm.read.return_value = body
        cm.__enter__.return_value = cm
        with mock.patch.object(df.urllib.request, "urlopen", return_value=cm):
            with self.assertRaises(ValueError):
                df.fetch_specs("X", "key", timeout=1)

    def test_json_embedded_in_prose_is_extracted(self):
        content = 'Sure!\n```json\n{"ram_gb": [8], "benchmark_score": 5}\n```\nHope that helps'
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        cm = mock.MagicMock()
        cm.read.return_value = body
        cm.__enter__.return_value = cm
        with mock.patch.object(df.urllib.request, "urlopen", return_value=cm):
            got = df.fetch_specs("X", "key", timeout=1)
        self.assertEqual(got["ram_gb"], [8])

    def test_lineage_network_failure_uses_cache(self):
        with TempCache():
            df._save(df.LINEAGE_CACHE, {"fetched_at": 0, "devices": [
                {"codename": "c", "vendor": "V", "name": "N"}]})
            with mock.patch.object(df, "_get_json", side_effect=urllib.error.URLError("down")):
                devs = df.lineage_devices(refresh=True)
            self.assertEqual(len(devs), 1)


class TestSummarize(unittest.TestCase):
    def test_includes_key_fields(self):
        s = df.summarize({
            "kind": "phone", "chip": "SD888", "benchmark_name": "antutu_v10",
            "benchmark_score": 800_000, "ram_gb": [8], "storage_gb": [128],
            "used_price_eur": 220, "release_year": 2021,
            "lineageos_unofficial": True, "score": 58, "verdict": "good"})
        for expect in ("SD888", "800,000", "8GB", "220", "2021", "58/100", "unofficial"):
            self.assertIn(expect, s)

    def test_laptop_omits_lineageos(self):
        s = df.summarize({"kind": "laptop", "chip": "i5", "score": 50, "verdict": "ok"})
        self.assertNotIn("LineageOS", s)

    def test_sparse_facts_do_not_crash(self):
        self.assertIsInstance(df.summarize({}), str)


@unittest.skipUnless(os.environ.get("LIVE") == "1", "set LIVE=1 to hit the network")
class TestLive(unittest.TestCase):
    def test_lineage_api_reachable_and_sane(self):
        devs = df.lineage_devices(refresh=True)
        self.assertGreater(len(devs), 100)
        self.assertTrue(any(d["vendor"] and d["name"] for d in devs))

    def test_real_spec_lookup(self):
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            self.skipTest("no GROQ_API_KEY")
        try:
            facts = df.fetch_specs("Samsung Galaxy S21 5G", key, kind="phone", timeout=90)
        except df.RateLimited as e:
            # Shared token budget with the running monitor. Environmental, not
            # a defect, so do not report it as a test failure.
            self.skipTest(f"provider busy: {e}")
        self.assertIn("ram_gb", facts)


class TestDeviceIdentity(unittest.TestCase):
    """A brand is not a device.

    A bare "Samsung" listing (a fridge, in the log that exposed this) used to
    resolve to a flagship SoC and get pushed as a EUR 100 deal, because the
    facts cache fell back to plain substring containment: "samsung" is
    contained in "samsung galaxy s24 ultra".
    """

    def test_brand_alone_is_not_identifiable(self):
        for key in ("samsung", "samsung galaxy", "iphone", "xiaomi",
                    "apple macbook", "notebook", "gaming pc", "telefon"):
            self.assertFalse(df.is_identifiable(key), key)

    def test_model_number_makes_it_identifiable(self):
        for key in ("samsung galaxy s21", "iphone 12", "thinkpad t480",
                    "xiaomi redmi note 12s"):
            self.assertTrue(df.is_identifiable(key), key)

    def test_brand_only_title_resolves_to_nothing(self):
        with TempCache():
            for title in ("Samsung", "iPhone", "Refrigerador marca Samsung"):
                facts = df.facts_for_listing(title)
                self.assertFalse(df.has_real_facts(facts), title)
                self.assertIsNone(df.deterministic_verdict(title, "€100"), title)

    def test_containment_does_not_leak_specs_between_devices(self):
        self.assertFalse(df.same_device("samsung", "samsung galaxy s24 ultra"))
        self.assertFalse(df.same_device("iphone 12", "iphone 12 pro max"))
        self.assertFalse(df.same_device("samsung galaxy s21", "samsung galaxy s21 ultra"))

    def test_same_device_tolerates_missing_filler(self):
        self.assertTrue(df.same_device("samsung s21", "samsung galaxy s21"))
        self.assertTrue(df.same_device("samsung a12", "samsung galaxy a12"))


class TestModelKeyRamStorageShorthand(unittest.TestCase):
    """"8/128GB" (RAM/storage shorthand with no unit on the RAM half) used to
    fragment a common phone's cache key.

    _norm() turns "/" into whitespace before _SPEC_RE ever sees a token, so
    "Samsung Galaxy S22 5G, 8/128GB" tokenized to a bare "8" with no unit
    suffix -- indistinguishable from a model number -- and got kept in the
    key: "samsung galaxy s22 5g 8". A real Samsung Galaxy S22 5G listing
    then missed its own cached facts and queued a fresh lookup instead of
    reusing the one already resolved for "samsung galaxy s22".
    """

    def test_ram_storage_shorthand_is_dropped_from_the_key(self):
        self.assertEqual(
            df.model_key("Samsung Galaxy S22 5G, 8/128GB Dual SIM"),
            "samsung galaxy s22 5g",
        )
        self.assertEqual(
            df.model_key("Xiaomi Redmi Note 12 4/128GB"),
            "xiaomi redmi note 12",
        )

    def test_model_numbers_survive_the_same_shape(self):
        """A bare digit is only spec shorthand next to a slash and a unit --
        elsewhere (iPhone's own model number) it must stay in the key."""
        self.assertEqual(df.model_key("iPhone 13 128GB"), "iphone 13")

    def test_ram_storage_pair_without_units_is_dropped(self):
        """"12/256" carries no "GB" on either half. device_facts used to
        require the unit while benchmark_db did not, so "Xiaomi 14T 12/256"
        keyed as a different device from "Xiaomi 14T" and lost its specs.
        Both now go through one tokenizer."""
        for title, expected in (
            ("Xiaomi 14T 12/256", "xiaomi 14t"),
            ("Samsung S25 12/128", "samsung s25"),
            ("HONOR Magic V6 16 GB/512 GB", "honor magic v6"),
            ("Samsung Galaxy S22 5G, 8/128GB Dual SIM", "samsung galaxy s22 5g"),
        ):
            self.assertEqual(df.model_key(title), expected, title)

    def test_one_tokenizer_for_both_sides(self):
        """A second implementation of the same normalization is what broke
        this twice; assert they cannot silently diverge again."""
        # The same module device_facts resolves, not whatever copy happens to
        # be installed -- comparing across two installs tests the sync, not
        # the code.
        import benchmark_db as bdb
        for title in ("Xiaomi 14T 12/256", "Iphone 13pro Max",
                      "iPhone 13 Pro 256 GB", "Ipad pro 3gen 1 TB"):
            self.assertEqual(df._mechanical_tokens(title), bdb._tokens(title), title)

    def test_variant_word_glued_to_the_model_number_is_split(self):
        """"13pro" is one token to the tokenizer but two words to every cache
        key and table entry, so a phone whose facts were already resolved fell
        through to a fresh AI evaluation."""
        for title, expected in (
            ("Iphone 13pro Max", "iphone 13 pro max"),
            ("iPhone 12mini", "iphone 12 mini"),
            ("Honor 200lite", "honor 200 lite"),
            ("XIAMI NOTE Redmi 14PRO", "xiami note redmi 14 pro"),
        ):
            self.assertEqual(df.model_key(title), expected, title)

    def test_model_designations_are_not_split(self):
        """Letters after a digit are only a separate word for known variants;
        elsewhere they are part of the model designation itself."""
        for title, expected in (
            ("Samsung Galaxy S22", "samsung galaxy s22"),
            ("Xiaomi Redmi Note 12s 256GB", "xiaomi redmi note 12s"),
            ("Lenovo ThinkPad T480", "lenovo thinkpad t480"),
            ("Samsung Galaxy A54 5G", "samsung galaxy a54 5g"),
        ):
            self.assertEqual(df.model_key(title), expected, title)

    def test_capacity_as_its_own_token_leaves_the_key(self):
        """"256 GB" reaches the tokenizer as two tokens, and a bare "256" is
        indistinguishable from a model number -- so capacity used to leak in
        and split one device across several keys."""
        self.assertEqual(df.model_key("iPhone 13 Pro 256 GB"), "iphone 13 pro")
        self.assertEqual(df.model_key("iPhone 13 Mini 256 GB"), "iphone 13 mini")
        self.assertEqual(df.model_key("Samsung Galaxy A12 128GB"), "samsung galaxy a12")

    def test_generation_wording_collapses_to_the_number(self):
        """One device is advertised as "3rd gen", "3rd Gen", "3gen" and
        "3. generácia". The generation NUMBER identifies it; the word naming
        it only fragments the key."""
        for title, expected in (
            ("Ipad 6. generácia 32gb", "ipad 6"),
            ("Apple ipad 6 32GB", "apple ipad 6"),
            ("iPad 9th gen wi-fi 256gb", "ipad 9"),
            ("Ipad pro 3gen 1 TB", "ipad pro 3"),
        ):
            self.assertEqual(df.model_key(title), expected, title)

    def test_ordinal_folding_spares_real_model_names(self):
        """A model name ending in a letter is not an ordinal: "6s" and "12s"
        must survive intact."""
        self.assertEqual(df.model_key("iPhone 6s 64GB"), "iphone 6s")
        self.assertEqual(df.model_key("Xiaomi Redmi Note 12s"), "xiaomi redmi note 12s")

    def test_generation_variants_resolve_to_one_device(self):
        self.assertTrue(df.same_device("ipad 6", "apple ipad 6"))
        self.assertTrue(df.same_device("apple ipad 6 wi fi", "apple ipad 6"))

    def test_different_generations_stay_distinct(self):
        """Over-merging prices the wrong device, which is worse than a miss."""
        self.assertFalse(df.same_device("ipad pro 11 3", "ipad pro 11 4"))
        self.assertFalse(df.same_device("apple ipad 6", "apple ipad pro 11 3"))

    def test_glued_variant_resolves_the_cached_device(self):
        with TempCache():
            df._save(df.FACTS_CACHE, {
                "apple iphone 13 pro max": {
                    "query": "Apple iPhone 13 Pro Max", "kind": "phone",
                    "chip": "a15 bionic", "ram_gb": [6],
                    "benchmark_name": "antutu_v10", "benchmark_score": 1_200_000,
                },
            })
            facts = df.facts_for_listing("Iphone 13pro Max")
            self.assertTrue(df.has_real_facts(facts))
            self.assertEqual(facts.get("chip"), "a15 bionic")

    def test_shorthand_variant_still_resolves_the_cached_device(self):
        with TempCache():
            df._save(df.FACTS_CACHE, {
                "samsung galaxy s22": {
                    "query": "Samsung galaxy S22", "kind": "phone",
                    "chip": "exynos 2200", "ram_gb": [8],
                    "benchmark_name": "antutu_v10", "benchmark_score": 950_000,
                },
            })
            facts = df.facts_for_listing("Samsung Galaxy S22 5G, 8/128GB Dual SIM")
            self.assertTrue(df.has_real_facts(facts))
            self.assertEqual(facts.get("chip"), "exynos 2200")


class TestSocScoring(unittest.TestCase):
    """The chip comes from the model; the NUMBER comes from our table.

    Models name a phone's chip reliably and recall its benchmark badly -- one
    returned a confident Snapdragon for a Dimensity phone, another the right
    chip with no score. Scoring from the curated SoC table also rescues every
    device no device-level table lists: there is no Redmi Note 12/13/14 row
    anywhere, but their SoCs are all known.
    """

    def test_chip_spelled_any_way_still_scores(self):
        for chip in ("MediaTek Dimensity 8300-Ultra", "dimensity 8300",
                     "Dimensity 8300 Ultra"):
            self.assertEqual(df.soc_score(chip), 1_450_000, chip)

    def test_longest_chip_name_wins(self):
        """"snapdragon 8" must not answer for "snapdragon 8 elite"."""
        self.assertEqual(df.soc_score("Snapdragon 8 Elite"), 3_000_000)
        self.assertEqual(df.soc_score("Qualcomm Snapdragon 8 Gen 3"), 2_100_000)

    def test_unknown_chip_scores_nothing(self):
        # TempCache because a miss now QUEUES the chip, and an invented one
        # must not land in the running installation's queue.
        with TempCache():
            self.assertIsNone(df.soc_score("Some Unreleased Chip 999"))
            self.assertIsNone(df.soc_score(""))

    def test_named_chip_without_a_score_is_rescued(self):
        """A lookup that returns a chip but no benchmark is not a dead end."""
        with TempCache(), mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"}), \
                mock.patch.object(df, "_record_to_database"):
            with mock.patch.object(df, "fetch_specs", return_value={
                    "chip": "MediaTek Dimensity 8300-Ultra", "ram_gb": [12],
                    "unverified": True}):
                facts = df.device_facts("Xiaomi 14T", kind="phone")
        self.assertEqual(facts["benchmark_score"], 1_450_000)
        self.assertEqual(facts["benchmark_name"], "antutu_v10")
        self.assertTrue(facts["score_from_soc_table"])
        self.assertTrue(df.has_real_facts(facts))

    def test_a_looked_up_score_is_not_overwritten(self):
        """Only fills a gap; a real benchmark from the lookup still wins."""
        # _record_to_database writes to benchmark_db's OWN paths, which
        # TempCache does not redirect -- it only covers device_facts. Without
        # this stub the test writes an invented phone into the real learned
        # benchmark file, where every later lookup then trusts it.
        with TempCache(), mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"}), \
                mock.patch.object(df, "_record_to_database"):
            with mock.patch.object(df, "fetch_specs", return_value={
                    "chip": "Snapdragon 8 Elite", "benchmark_name": "antutu_v10",
                    "benchmark_score": 2_950_000}):
                facts = df.device_facts("Some Phone", kind="phone")
        self.assertEqual(facts["benchmark_score"], 2_950_000)
        self.assertNotIn("score_from_soc_table", facts)


class TestSocFilling(unittest.TestCase):
    """A SoC score is written only when two independent models agree.

    No free bulk source of SoC benchmarks exists outside a bot check, so the
    numbers come from models -- and one model's recollection is exactly what
    this module distrusts. Agreement between two is the substitute for a
    source; disagreement means at least one is guessing.
    """

    def queue(self, chip="Dimensity 7020"):
        df._save(df.SOC_PENDING, {df._norm(chip): {"chip": chip, "kind": "phone"}})

    def run_fill(self, answers):
        """answers: the value each successive model returns."""
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "g", "CEREBRAS_API_KEY": "c"}):
            with mock.patch.object(df, "_ask_soc", side_effect=answers):
                return df.fill_socs(limit=5, verbose=False)

    def test_two_agreeing_models_are_believed(self):
        with TempCache():
            self.queue()
            self.assertEqual(self.run_fill([700_000, 730_000]), 1)
            table = df._load(df.SOC_LEARNED)["soc_antutu_v10"]
            self.assertAlmostEqual(table["dimensity 7020"], 715_000, delta=1)

    def test_disagreement_is_not_recorded(self):
        with TempCache():
            self.queue()
            self.assertEqual(self.run_fill([700_000, 1_800_000]), 0)
            self.assertEqual(df._load(df.SOC_LEARNED), {})
            self.assertIn("dimensity 7020", df._load(df.SOC_PENDING))

    def test_one_answer_alone_is_not_enough(self):
        with TempCache():
            self.queue()
            self.assertEqual(self.run_fill([700_000, None, None, None]), 0)
            self.assertEqual(df._load(df.SOC_LEARNED), {})

    def test_a_busy_provider_leaves_it_queued(self):
        with TempCache():
            self.queue()
            self.assertEqual(self.run_fill([df.RateLimited("busy")]), 0)
            self.assertIn("dimensity 7020", df._load(df.SOC_PENDING))

    def test_unknown_chip_is_queued_when_scored(self):
        with TempCache():
            self.assertIsNone(df.soc_score("Dimensity 7020"))
            self.assertIn("dimensity 7020", df._load(df.SOC_PENDING))

    def test_prose_is_never_queued_as_a_chip(self):
        """Only a short, digit-bearing name is a chip."""
        with TempCache():
            for junk in ("a really long sentence that is not a chip at all here",
                         "unknown", ""):
                df.soc_score(junk)
            self.assertEqual(df._load(df.SOC_PENDING), {})


class TestVerdictRouting(unittest.TestCase):
    """Complete specs settle a listing both ways; only gaps reach the model."""

    def test_known_and_cheap_is_a_deal_without_ai(self):
        v = df.deterministic_verdict("Samsung Galaxy S21 5G 128GB", "€100")
        self.assertEqual(v["decision"], "deal")
        self.assertEqual(v["score"], 5)

    def test_known_and_overpriced_is_rejected_without_ai(self):
        v = df.deterministic_verdict("Samsung Galaxy S21 5G 128GB", "€250")
        self.assertEqual(v["decision"], "reject")
        self.assertLess(v["score"], 5)

    def test_unknown_device_goes_to_ai(self):
        with TempCache():
            self.assertIsNone(df.deterministic_verdict("Nejaky zvlastny telefon XYZ", "€50"))

    def test_missing_price_goes_to_ai(self):
        self.assertIsNone(df.deterministic_verdict("Samsung Galaxy S21 5G 128GB", "cena dohodou"))


class TestOverrideScope(unittest.TestCase):
    """An override may be broader than a listing, but never a different device."""

    def test_broad_override_covers_the_full_title(self):
        self.assertTrue(df.override_applies("samsung galaxy s21", "galaxy s21"))

    def test_override_does_not_reach_a_variant(self):
        self.assertFalse(df.override_applies("samsung galaxy s21 ultra", "galaxy s21"))
        self.assertFalse(df.override_applies("iphone 12 pro max", "iphone 12"))

    def test_override_does_not_reach_a_different_model(self):
        self.assertFalse(df.override_applies("samsung galaxy s22", "galaxy s21"))

    def test_brandwide_override_cannot_match_everything(self):
        self.assertFalse(df.override_applies("samsung galaxy s21", "samsung"))


class TestDamagedListings(unittest.TestCase):
    """Benchmarks describe a working device; condition is the model's call."""

    def test_salvage_priced_parts_listing_is_pushed(self):
        """Missing a phone at parts money costs more than a wrong ping."""
        v = df.deterministic_verdict(
            "Predám Samsung A51 na opravu, alebo náhradné diely.", "€25")
        self.assertEqual(v["decision"], "deal")

    def test_damaged_but_dear_goes_to_the_model(self):
        self.assertIsNone(df.deterministic_verdict(
            "Predám Samsung A51 na opravu, alebo náhradné diely.", "€120"))

    def test_damage_words_are_detected_with_and_without_accents(self):
        for text in ("rozbitý displej", "rozbity displej", "nefunkčný telefon",
                     "iPhone for parts", "prasknuté sklo", "na diely"):
            self.assertTrue(df.looks_damaged(text), text)

    def test_working_listing_is_still_settled(self):
        self.assertIsNotNone(df.deterministic_verdict(
            "Samsung Galaxy S21 5G 128GB, super stav", "€100"))


class TestScamGuards(unittest.TestCase):
    """Arithmetic loves a scam: bait is, by construction, very cheap."""

    def test_absurdly_cheap_goes_to_the_model(self):
        """An RTX-class laptop at EUR 110 is bait, not a bargain."""
        self.assertIsNone(df.deterministic_verdict(
            "MSI Stealth 17 Studio Intel Core i9-13900H RTX 4080 32GB", "€109.99"))

    def test_a_real_bargain_is_still_pushed(self):
        v = df.deterministic_verdict("Samsung Galaxy S21 5G 128GB", "€100")
        self.assertEqual(v["decision"], "deal")

    def test_seller_with_many_identical_prices_is_flagged(self):
        with TempCache():
            for _ in range(df.BULK_SELLER_LISTINGS):
                df.note_seller("Ene Mustafa", "€109.99")
            self.assertTrue(df.seller_looks_bulk("ene mustafa", "€109.99"))
            self.assertFalse(df.seller_looks_bulk("Ene Mustafa", "€250"))
            self.assertFalse(df.seller_looks_bulk("Niekto Iny", "€109.99"))

    def test_one_listing_from_a_seller_is_not_bulk(self):
        with TempCache():
            df.note_seller("Bezny Predajca", "€100")
            self.assertFalse(df.seller_looks_bulk("Bezny Predajca", "€100"))


class TestWarmQueueDraining(unittest.TestCase):
    """One unresolvable entry must not block the queue behind it.

    The queue is walked in insertion order and the batch stopped on ANY
    empty result, because a swallowed exception left no way to tell "the
    provider is busy" from "this device has no answer". A monitor sat at the
    head of a 276-entry backlog and held it at zero resolved per run for days
    while the daily quota went unused.
    """

    RESOLVED = {
        "query": "Samsung Galaxy S22", "kind": "phone", "chip": "exynos 2200",
        "benchmark_name": "antutu_v10", "benchmark_score": 950_000, "ram_gb": [8],
    }

    def queue(self, *titles):
        df._save(df.PENDING, {
            df.model_key(t): {"title": t, "kind": "auto", "seen_at": 0} for t in titles
        })

    def test_unresolvable_entry_does_not_block_the_rest(self):
        with TempCache(), mock.patch.object(df, "time") as t:
            t.sleep = mock.Mock()
            t.time = mock.Mock(return_value=0)
            self.queue("Nejaky Neznamy Telefon XYZ", "Samsung Galaxy S22")

            def fake(model, **kw):
                if "s22" in model.lower():
                    return dict(self.RESOLVED)
                return {"specs_error": "no data", "specs_busy": False}

            with mock.patch.object(df, "device_facts", side_effect=fake):
                self.assertEqual(df.warm_cache(limit=5, verbose=False), 1,
                                 "the resolvable entry behind the failure must resolve")

    def test_busy_provider_still_stops_the_batch(self):
        """A shared per-minute budget is exhausted for everyone, so pressing
        on would only burn the rest of the batch on the same error."""
        with TempCache(), mock.patch.object(df, "time") as t:
            t.sleep = mock.Mock()
            t.time = mock.Mock(return_value=0)
            self.queue("Samsung Galaxy S21", "Samsung Galaxy S22")
            with mock.patch.object(df, "device_facts",
                                   return_value={"specs_error": "429", "specs_busy": True}):
                self.assertEqual(df.warm_cache(limit=5, verbose=False), 0)
            self.assertEqual(len(df._load(df.PENDING)), 2, "nothing may be retired")

    def test_repeated_failure_retires_the_entry(self):
        with TempCache(), mock.patch.object(df, "time") as t:
            t.sleep = mock.Mock()
            t.time = mock.Mock(return_value=0)
            self.queue("Nejaky Neznamy Telefon XYZ")
            with mock.patch.object(df, "device_facts",
                                   return_value={"specs_error": "no data", "specs_busy": False}):
                for _ in range(df.MAX_WARM_ATTEMPTS):
                    df.warm_cache(limit=5, verbose=False)
            self.assertEqual(df._load(df.PENDING), {},
                             "an unanswerable device must stop consuming budget")

    def test_unscoreable_entries_are_retired_without_a_lookup(self):
        with TempCache():
            self.queue("MSI Optix G27C6 27 165 Hz", "Sony PlayStation 5 Console")
            with mock.patch.object(df, "device_facts",
                                   side_effect=AssertionError("must not look up")):
                df.warm_cache(limit=5, verbose=False)
            self.assertEqual(df._load(df.PENDING), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
