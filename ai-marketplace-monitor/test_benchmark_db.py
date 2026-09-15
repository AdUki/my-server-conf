#!/usr/bin/env python3
"""Matching tests for benchmark_db (no network; uses a fixed mini-table)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from ai_marketplace_monitor import benchmark_db as b  # noqa: E402

DEVICES = {
    "Samsung Galaxy S21 5G (Exynos)": 11972,
    "Samsung Galaxy S21 Ultra 5G": 13500,
    "Samsung Galaxy A54": 12026,
    "Huawei P30": 8680,
    "Huawei P30 Pro": 9587,
    "Xiaomi 12": 16114,
    "Xiaomi Redmi Note 11": 4000,
}
CPUS = {
    "Intel Core i5-8350U @ 1.70GHz": 6085,
    "Intel Core i5-7300U @ 2.60GHz": 3620,
    "Intel Core i5-1135G7 @ 2.40GHz": 9300,
    "Intel Core i5-13450HX": 24437,
    "AMD Ryzen 5 5500U": 13000,
}


class TestDeviceMatching(unittest.TestCase):
    def m(self, q):
        got = b.match_device(q, DEVICES)
        return got[0] if got else None

    def test_exact(self):
        self.assertEqual(self.m("Samsung Galaxy A54"), "Samsung Galaxy A54")

    def test_missing_filler_word_still_matches(self):
        """'Samsung S21' is the same phone as 'Samsung Galaxy S21'."""
        self.assertEqual(self.m("Samsung S21"), "Samsung Galaxy S21 5G (Exynos)")

    def test_case_and_accents_ignored(self):
        self.assertEqual(self.m("PREDÁM samsung galaxy a54 super stav"),
                         "Samsung Galaxy A54")

    def test_capacity_pair_ignored(self):
        for q in ("samsung a54 8/256", "Samsung A54 8GB/256GB", "Samsung A54 8 GB / 256 GB"):
            self.assertEqual(self.m(q), "Samsung Galaxy A54", q)

    def test_variant_is_not_the_base_model(self):
        self.assertEqual(self.m("Samsung Galaxy S21 Ultra"), "Samsung Galaxy S21 Ultra 5G")
        self.assertEqual(self.m("Huawei P30 Pro"), "Huawei P30 Pro")
        self.assertEqual(self.m("Huawei P30"), "Huawei P30")

    def test_different_model_number_never_matches(self):
        self.assertIsNone(self.m("Samsung Galaxy S22"))
        self.assertIsNone(self.m("Xiaomi 13"))

    def test_absent_model_returns_none_not_a_guess(self):
        """A wrong benchmark is worse than no benchmark."""
        self.assertIsNone(self.m("Xiaomi Redmi Note 12 Pro"))
        self.assertIsNone(self.m("Samsung Galaxy A05s"))

    def test_note_line_not_confused_with_base(self):
        self.assertIsNone(self.m("Xiaomi 11"))
        self.assertEqual(self.m("Xiaomi Redmi Note 11"), "Xiaomi Redmi Note 11")

    def test_empty_query(self):
        self.assertIsNone(self.m(""))


class TestCpuMatching(unittest.TestCase):
    def m(self, q):
        got = b.match_component(q, CPUS)
        return got[0] if got else None

    def test_cpu_inside_a_long_listing(self):
        self.assertEqual(self.m("Lenovo ThinkPad T480 i5-8350U 16GB"),
                         "Intel Core i5-8350U @ 1.70GHz")

    def test_slovak_listing_with_slashes(self):
        self.assertEqual(self.m("Dell Latitude 5580 / i5 7300U / 8GB DDR4 / 256GB NVME"),
                         "Intel Core i5-7300U @ 2.60GHz")

    def test_vendor_words_optional(self):
        self.assertEqual(self.m("HP 15, 11th Gen Intel Core i5-1135G7, 16GB RAM"),
                         "Intel Core i5-1135G7 @ 2.40GHz")

    def test_hx_part_number(self):
        self.assertEqual(self.m("Herný notebook Lenovo LOQ - i5-13450HX RTX 4050"),
                         "Intel Core i5-13450HX")

    def test_amd(self):
        self.assertEqual(self.m("Acer Aspire 5 Ryzen 5 5500U 16GB"), "AMD Ryzen 5 5500U")

    def test_no_cpu_named(self):
        self.assertIsNone(self.m("Lenovo ThinkPad, dobry stav"))


class TestJunkEntriesCannotHijack(unittest.TestCase):
    """An entry naming only a variant and a number identifies no product.

    The device table carries a handful ("Pro 15", "s24", "i15proMax"). Against
    a real listing the only word such an entry leaves unexplained is the
    BRAND -- the most discriminating word there is -- so "iPhone 15 Pro"
    scored 0.67 against "Pro 15" and came back as a 60,500-AnTuTu device,
    marking a flagship "not worth buying at any price".
    """

    TABLE = {"Pro 15": 726, "s24": 500, "Samsung Galaxy A54": 12026,
             "Samsung Galaxy S21 5G (Exynos)": 11972}

    def test_brandless_junk_entry_is_refused(self):
        self.assertIsNone(b.match_device("iPhone 15 Pro 128Gb", self.TABLE))
        self.assertIsNone(b.match_device("Samsung Galaxy S24 128GB", self.TABLE))

    def test_real_entries_still_match(self):
        self.assertEqual(b.match_device("Samsung Galaxy A54", self.TABLE)[0],
                         "Samsung Galaxy A54")
        self.assertEqual(b.match_device("Samsung Galaxy S21 128GB", self.TABLE)[0],
                         "Samsung Galaxy S21 5G (Exynos)")

    def test_brand_mismatch_is_refused(self):
        self.assertIsNone(
            b.match_device("Xiaomi Redmi A54", {"Samsung Galaxy A54": 12026}))


class TestIosGenerations(unittest.TestCase):
    """Apple reuses one product name across generations.

    The table spells the generation out, so a phrase search finds the
    generation-less "Apple iPad Pro 12.9" inside a 5th-generation listing and
    returns the 2015 model's score -- an order of magnitude low, which reads
    as "too weak to buy at any price". lookup_device_ios matches strictly.
    """

    TABLE = {
        "Apple iPad Pro 12.9 (Cellular)": 2120,
        "Apple iPad Pro 12.9 4th Gen (WiFi)": 8912,
        "Apple iPad Pro 11.0 5th Gen (Cellular)": 23475,
        "Apple iPhone 13 Pro": 10003,
    }

    def m(self, q):
        got = b.match_device(q, self.TABLE)
        return got[0] if got else None

    def test_generation_is_not_dropped(self):
        self.assertEqual(self.m("Apple iPad Pro 12.9 4th Gen"),
                         "Apple iPad Pro 12.9 4th Gen (WiFi)")

    def test_older_entry_cannot_stand_in_for_a_newer_generation(self):
        """Better to return nothing than the 2015 model's score."""
        self.assertNotEqual(self.m("iPad Pro 12.9 5th gen 256gb"),
                            "Apple iPad Pro 12.9 (Cellular)")

    def test_plain_model_still_matches(self):
        self.assertEqual(self.m("iPhone 13 Pro 256 GB"), "Apple iPhone 13 Pro")


class TestSlovakTransliteration(unittest.TestCase):
    """Every Slovak diacritic must fold to its plain ASCII letter."""

    PAIRS = [
        ("á", "a"), ("ä", "a"), ("č", "c"), ("ď", "d"), ("é", "e"), ("í", "i"),
        ("ĺ", "l"), ("ľ", "l"), ("ň", "n"), ("ó", "o"), ("ô", "o"), ("ŕ", "r"),
        ("š", "s"), ("ť", "t"), ("ú", "u"), ("ý", "y"), ("ž", "z"),
    ]

    def test_every_letter_lower_and_upper(self):
        for ch, plain in self.PAIRS:
            self.assertEqual(b._norm(ch), plain, ch)
            self.assertEqual(b._norm(ch.upper()), plain, ch.upper())

    def test_no_letter_is_dropped(self):
        for ch, _ in self.PAIRS:
            self.assertTrue(b._norm(ch), f"{ch} vanished")

    def test_real_words(self):
        for word, plain in [
            ("Predám", "predam"), ("Úplne nový", "uplne novy"),
            ("Sušička", "susicka"), ("nefunkčný", "nefunkcny"),
            ("Príslušenstvo", "prislusenstvo"), ("čierny", "cierny"),
        ]:
            self.assertEqual(b._norm(word), plain, word)

    def test_accented_listing_matches_plain_table_entry(self):
        got = b.match_device("Predám Samsung Galaxy A54 – zánovný", DEVICES)
        self.assertEqual(got[0], "Samsung Galaxy A54")


class TestLearnedEntries(unittest.TestCase):
    """Web lookups must enrich the local database, and only when sane."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = (b.CACHE_DIR, b.LOCAL_PATH, b.DB_PATH)
        b.CACHE_DIR = Path(self.tmp.name)
        b.LOCAL_PATH = b.CACHE_DIR / "benchmarks_local.json"
        b.DB_PATH = b.CACHE_DIR / "benchmarks.json"
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        b.CACHE_DIR, b.LOCAL_PATH, b.DB_PATH = self._orig

    def test_plausible_value_is_stored(self):
        self.assertTrue(b.add_entry("device", "Honor 8X", 5200))
        self.assertEqual(b.load_local()["device"]["Honor 8X"], 5200)

    def test_implausible_values_are_refused(self):
        for kind, bad in (("device", 9_000_000), ("cpu", -3), ("antutu", 5),
                          ("gpu", 0)):
            self.assertFalse(b.add_entry(kind, "Bogus", bad), (kind, bad))
        self.assertEqual(b.load_local(), {})

    def test_bool_is_not_a_score(self):
        self.assertFalse(b.add_entry("device", "Bogus", True))

    def test_empty_name_refused(self):
        self.assertFalse(b.add_entry("device", "   ", 5000))

    def test_learned_entry_is_matchable(self):
        b.add_entry("device", "Samsung Galaxy A05s", 4200)
        b.save({"device": {}})           # upstream table has nothing
        got = b.match_device("Predám Samsung Galaxy A05s 4/64",
                             b.load(auto_download=False).get("device", {}))
        self.assertIsNotNone(got)
        self.assertEqual(got[1], 4200)

    def test_local_survives_upstream_refresh(self):
        b.add_entry("device", "Honor 8X", 5200)
        b.save({"device": {"Something Else": 1}})   # simulate a re-download
        self.assertEqual(b.load(auto_download=False)["device"]["Honor 8X"], 5200)

    def test_local_overrides_upstream(self):
        b.save({"device": {"Honor 8X": 1}})
        b.add_entry("device", "Honor 8X", 5200)
        self.assertEqual(b.load(auto_download=False)["device"]["Honor 8X"], 5200)


class TestTextPreparation(unittest.TestCase):
    """Index entries and listing text must be prepared identically."""

    def test_prepare_folds_case_accents_and_punctuation(self):
        self.assertEqual(b.prepare("Predám  Galaxy-S21, 5G!"),
                         ["predam", "galaxy", "s21", "5g"])

    def test_prepare_collapses_runs_of_spaces(self):
        self.assertEqual(b.prepare("Samsung   Galaxy    S21"),
                         ["samsung", "galaxy", "s21"])

    def test_prepare_joins_capacities(self):
        self.assertEqual(b.prepare("Galaxy S21 128 GB"),
                         ["galaxy", "s21", "128gb"])


class TestPhraseIndex(unittest.TestCase):
    TABLE = {
        "Samsung Galaxy S21 5G (Exynos)": 11972,
        "Samsung Galaxy S21 Ultra 5G": 14000,
        "Pro 15": 726,
        "Intel Core i5-8350U @ 1.70GHz": 6085,
        "AMD Ryzen 5 5600X": 21824,
    }

    def find(self, text):
        return b.find_in_text(text, "test-%d" % id(self), dict(self.TABLE))

    def test_finds_the_model_named_in_a_description(self):
        hit = self.find("Samsung. Predam telefon, je to Galaxy S21 5G, 128GB, super stav")
        self.assertEqual(hit[0], "Samsung Galaxy S21 5G (Exynos)")

    def test_word_order_is_respected(self):
        """"iPhone 15 Pro" must not match an entry named "Pro 15"."""
        self.assertIsNone(self.find("iPhone 15 Pro 128Gb TOP STAV"))

    def test_variant_is_not_valued_as_the_base_model(self):
        hit = self.find("Predam Samsung Galaxy S21 Ultra 256GB")
        self.assertEqual(hit[0], "Samsung Galaxy S21 Ultra 5G")

    def test_base_model_is_not_valued_as_the_variant(self):
        hit = self.find("Samsung Galaxy S21 5G 128GB")
        self.assertEqual(hit[0], "Samsung Galaxy S21 5G (Exynos)")

    def test_brand_alone_matches_nothing(self):
        for text in ("Samsung", "Refrigerador marca Samsung", "predam telefon"):
            self.assertIsNone(self.find(text), text)

    def test_vendor_boilerplate_is_optional(self):
        self.assertEqual(self.find("ThinkPad T480 s i5-8350U")[0],
                         "Intel Core i5-8350U @ 1.70GHz")
        self.assertEqual(self.find("Herny PC Ryzen 5 5600X + RTX 3060")[0],
                         "AMD Ryzen 5 5600X")


class TestCpuTableCoverage(unittest.TestCase):
    """cpu_list.php serves Intel only; the shipped table must not."""

    def test_amd_is_present_if_a_table_is_downloaded(self):
        cpu = b.load(auto_download=False).get("cpu", {})
        if not cpu:
            self.skipTest("no downloaded CPU table on this machine")
        self.assertTrue(any("Ryzen" in name for name in cpu),
                        "CPU table has no AMD parts: the Intel-only page is back")


if __name__ == "__main__":
    unittest.main(verbosity=1)
