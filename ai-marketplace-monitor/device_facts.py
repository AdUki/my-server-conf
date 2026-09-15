#!/usr/bin/env python3
"""Look up hard facts about a phone model once, then cache them forever.

Why this exists
---------------
The LLM was guessing specs and getting them wrong (it rejected a Galaxy S21
for "failing the 600k AnTuTu floor" and for lacking "custom ROM
significance" -- both false). Asking the model to web-search on every
listing fixes accuracy but is expensive: search results are token-heavy and
the same handful of phone models repeat across hundreds of listings.

So: resolve facts per MODEL, cache them on disk, and let the per-listing
evaluation stay on a cheap model that is handed the facts rather than
recalling them.

Sources
-------
LineageOS support : the project's own build API. NOTE this covers OFFICIAL
                    builds only. Verified against the full wiki archive (735
                    device files, discontinued ones included): a device with
                    only unofficial/XDA builds -- e.g. the Galaxy S21 -- does
                    not appear at all. "not official" therefore does NOT mean
                    "cannot run LineageOS".
AnTuTu / RAM      : Groq's agentic `groq/compound` model, which performs real
                    web searches server-side. Spec sites (kimovil, nanoreview)
                    return HTTP 403 to scripts, so scraping them directly is
                    not an option.

Usage
-----
    ./device_facts.py "Samsung Galaxy S21 5G"
    ./device_facts.py --refresh "Xiaomi Redmi Note 12"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # inside the package
    from .utils import amm_home as CACHE_DIR
except ImportError:  # running the file directly
    CACHE_DIR = Path.home() / ".ai-marketplace-monitor"
FACTS_CACHE = CACHE_DIR / "device_facts.json"
# Hand-maintained truth that beats anything looked up. For facts you know and
# the automated sources get wrong or cannot see (unofficial ROM support,
# benchmark scores the model misremembers). Same shape as a cache entry.
OVERRIDES = CACHE_DIR / "device_facts_overrides.json"
# Models seen during evaluation that have no cached facts yet. Evaluation is
# never allowed to block on a slow web lookup, so misses are recorded here and
# resolved later by `ai-marketplace-monitor --warm-facts`.
PENDING = CACHE_DIR / "device_facts_pending.json"
# SoC benchmark scores learned after install, and the chips still waiting for
# one. Scoring per CHIP rather than per DEVICE is what covers phones no table
# lists -- there is no Redmi Note 14 row anywhere, but its SoC serves every
# Redmi Note 14 variant and every other phone that ships the same chip.
SOC_LEARNED = CACHE_DIR / "soc_scores.json"
SOC_PENDING = CACHE_DIR / "soc_pending.json"
LINEAGE_CACHE = CACHE_DIR / "lineageos_devices.json"
LINEAGE_TTL = 7 * 24 * 3600  # the supported-device list changes slowly
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Agentic models with server-side web search. compound-mini is tried first:
# full compound pulls in far more search context and gets rejected outright
# (HTTP 413) whenever the per-minute token budget is partly used, which on a
# free tier is most of the time.
GROQ_SEARCH_MODELS = ("groq/compound-mini", "groq/compound")
# Plain (non-agentic) models, tried only after the searching ones are busy.
# They answer from training data instead of the live web, so the result is
# marked unverified -- but a known RAM figure and a roughly right benchmark
# beat no spec line at all, and these have their own separate rate limits.
GROQ_FALLBACK_MODELS = ("openai/gpt-oss-120b", "qwen/qwen3.6-27b")
# Cerebras: a separate free tier with its own allowance. No web search, so its
# answers are recollection and are marked unverified like the Groq fallbacks --
# but a named CHIP is worth having even unverified, because the SCORE then
# comes from the curated SoC table rather than from the model.
CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"
# Verified live against /v1/models on this key. gemma is listed first for
# SoC cross-checking: Groq's fallbacks are gpt-oss and qwen, so pairing a
# DIFFERENT family means agreement is two independent recollections rather
# than the same weights answering twice.
CEREBRAS_MODELS = ("gemma-4-31b", "qwen-3.8-27b", "gpt-oss-120b")
# Google, through its OpenAI-compatible endpoint, so the same client code
# reaches it. A different company's weights entirely, which is what makes it
# worth having for cross-checking a SoC score: two Groq models agreeing is
# two recollections from one lab, and the free key for this is already here.
GEMINI_OPENAI_URL = ("https://generativelanguage.googleapis.com"
                     "/v1beta/openai/chat/completions")
GEMINI_MODELS = ("gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite")
UA = "ai-marketplace-monitor device-facts"


# Providers that answered "payment required" this run. A 402 is not going to
# fix itself on retry, so the endpoint is dropped rather than tried for every
# chip: a free-tier key that authenticates but has no inference quota would
# otherwise waste the first attempt of every single lookup.
_DISABLED_ENDPOINTS: set = set()


class RateLimited(RuntimeError):
    """The provider is busy (HTTP 413/429). Retry later; nothing is wrong."""


def _get_json(url: str, timeout: int = 30) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _load(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(path)  # atomic, so a crash mid-write cannot corrupt the cache


# --------------------------------------------------------------------------
# LineageOS
# --------------------------------------------------------------------------
def lineage_devices(refresh: bool = False) -> List[Dict[str, str]]:
    """Return [{codename, vendor, name}] for every LineageOS-supported device.

    Uses the official build API, which returns vendor + marketing name (e.g.
    "Samsung" / "Galaxy S21 5G"). The wiki's _data/devices/*.yml files carry
    the same info but only as ~800 separate files, and the wiki has no JSON
    index (both documented index URLs 404), so this endpoint is the only
    cheap source of names that can be matched against a listing title.
    """
    cached = _load(LINEAGE_CACHE)
    if not refresh and cached.get("fetched_at", 0) + LINEAGE_TTL > time.time():
        return cached.get("devices", [])

    try:
        oems = _get_json("https://download.lineageos.org/api/v2/oems")
    except (urllib.error.URLError, ValueError) as e:
        print(f"warn: LineageOS device list unavailable ({e}); using cache", file=sys.stderr)
        return cached.get("devices", [])

    devices = []
    for oem in oems or []:
        vendor = oem.get("name") or oem.get("oem") or ""
        for d in oem.get("devices", []) or []:
            devices.append(
                {
                    "codename": d.get("model", ""),
                    "vendor": vendor,
                    "name": d.get("name", ""),
                }
            )
    if devices:
        _save(LINEAGE_CACHE, {"fetched_at": time.time(), "devices": devices})
        return devices
    return cached.get("devices", [])


# Words that appear in listing titles but say nothing about which model it is.
# Without stripping these, "Predam Samsung Galaxy S21 5G 128GB" and "Samsung
# Galaxy S21" become two different cache keys for one device -- which meant a
# web lookup per LISTING instead of per MODEL, exhausting the daily request
# quota. Slovak/Czech included, since that is what the listings are written in.
_NOISE = {
    # "nova"/"nová" deliberately absent: it is Huawei's product line as well
    # as Slovak for "new", and treating it as noise merges nova into whatever
    # else the brand sells. See benchmark_db._LISTING_NOISE.
    "predam", "predám", "predaj", "novy", "nový", "nove", "nové",
    "uplne", "úplne", "super", "stav", "zanovny", "zánovný", "top", "lacno",
    "ako", "novy!", "cierny", "čierny", "biely", "modry", "modrý", "zeleny",
    "cierna", "biela", "sivy", "sivá", "zlty", "zlatý", "gold", "black", "white",
    "blue", "green", "grey", "gray", "silver", "red", "pink", "purple",
    "dual", "sim", "dualsim", "esim", "nabijacka", "zaruka", "záruka",
    "faktura", "faktúra", "used", "new", "mint", "like", "condition",
    "phone", "telefon", "telefón", "mobil", "smartfon", "smartfón", "handy",
    "laptop", "notebook", "pc", "eur", "e", "s", "v", "so", "za",
    # Generation wording. The GENERATION NUMBER identifies the device and is
    # kept; the word naming it is written a dozen ways for the same product
    # ("3rd gen", "3rd Gen", "3gen", "3. generácia", "4th Generation") and
    # only fragments the key. "inch"/"palcovy" go the same way: a screen size
    # is already carried by its number.
    "gen", "generation", "generacia", "generacie", "generacii", "generacnej",
    "inch", "palcovy", "zoll",
}
# Capacity/spec tokens that survive tokenization ("128gb"): variant detail,
# not model identity. RAM/storage PAIRS ("8/128GB", "12/256") are removed
# earlier, by the shared tokenizer, which still sees the slash -- by the time
# a token reaches this regex the separator is gone and a bare "12" is
# indistinguishable from a model number.
_SPEC_RE = re.compile(r"^\d+(gb|tb|mb|mah|hz|ram|w)$|^\d+/\d+$|^\d+gb\d+gb$")
# Sellers glue the model number to the variant word: "13pro", "12mini",
# "200lite", "15Pro", "17Air", "14PRO". Splitting that is done by the shared
# tokenizer (benchmark_db._GLUED_VARIANT_RE); the fallback below is only for
# running this file with no benchmark_db importable at all.
_GLUED_VARIANT_RE = re.compile(
    r"(?<=\d)(?=(?:pro|max|mini|lite|ultra|plus|note|edge|fold|flip|air|neo|prime|xl"
    r"|gen|generation)\b)",
    re.IGNORECASE,
)
# English ordinal suffix on a generation number: "6th" and "6" are the same
# device. Folded to the bare number so "iPad 9th gen" and "iPad 9" agree.
# Deliberately anchored to a following word boundary, so a real model name
# that ends in a letter ("iPhone 6s", "Redmi Note 12s") is untouched.
_ORDINAL_RE = re.compile(r"\b(\d{1,2})\s*(?:st|nd|rd|th)\b", re.IGNORECASE)
# Battery health, discounts: "100%", "87 %". Never part of a model name.
_PERCENT_RE = re.compile(r"\d+\s*%")


def _mechanical_tokens(text: str) -> List[str]:
    """Tokenize exactly the way the benchmark tables do.

    This used to be a second, parallel implementation of the same idea, and
    the two drifted: benchmark_db already treated "12/256" (no unit on either
    half) as RAM/storage, while the copy here required a "GB" and so kept
    "12" and "256" as if they were model numbers -- which made
    "Xiaomi 14T 12/256" a different device from "Xiaomi 14T" and cost it its
    specs. Every such divergence is a device that silently loses its facts,
    so there is now ONE tokenizer and this delegates to it.
    """
    try:
        from .benchmark_db import _tokens
    except ImportError:  # run as a standalone file, not a package member
        try:
            from benchmark_db import _tokens
        except ImportError:  # pragma: no cover - degrade, never crash
            return _norm(_GLUED_VARIANT_RE.sub(" ", text)).split()
    return _tokens(text)


def _trim_to_model(tokens: List[str]) -> List[str]:
    """Drop the prose that trails the model name.

    A listing ends in whatever the seller wanted to say -- "v záruke", "top
    stav", "100% zdravie batérie", "NA OPRAVU". Matching those against a word
    list cannot work in Slovak, where every noun declines: _NOISE knew
    "zaruka" and "záruka" but not "záruke", so "Xiaomi 14T 12/256 v záruke"
    keyed as "xiaomi 14t zaruke" and missed the device's own facts. The list
    would need every case of every word, forever. Structure settles it
    instead -- see benchmark_db.trim_to_model, which the table lookups use for
    the same reason and which this delegates to so the two cannot diverge.
    """
    try:
        from .benchmark_db import trim_to_model
    except ImportError:  # run as a standalone file, not a package member
        try:
            from benchmark_db import trim_to_model
        except ImportError:  # pragma: no cover - degrade, never crash
            return tokens
    return trim_to_model(tokens)


def model_key(text: str) -> str:
    """Reduce a listing title to a stable per-model cache key.

    "Predám Samsung Galaxy S21 5G 128GB, super stav" -> "samsung galaxy s21 5g"

    Keeps brand and model tokens, drops sales patter, colours, capacities and
    condition words. Imperfect by nature -- sellers write whatever they like --
    but it collapses the many ways one device is advertised into a single
    lookup, which is the whole point of the cache.
    """
    # A percentage is battery health or a discount, never model identity --
    # and it survives as a bare digit-bearing token ("100% zdravie" -> "100"),
    # which is exactly what _trim_to_model treats as the end of a model name.
    text = _PERCENT_RE.sub(" ", text)
    text = _ORDINAL_RE.sub(r"\1", text)
    tokens = []
    for tok in _mechanical_tokens(text):
        if tok in _NOISE or _SPEC_RE.match(tok):
            continue
        tokens.append(tok)
        if len(tokens) >= 5:  # brand + model is short; the rest is description
            break
    return " ".join(_trim_to_model(tokens)) or _norm(text)


def _norm(s: str) -> str:
    """Lowercase, strip accents, collapse to alphanumerics.

    Accents are folded rather than dropped: naively removing non-ASCII turned
    "Predám" into "pred m" (two meaningless tokens) instead of "predam", so
    the Slovak sales words never matched the noise list.
    """
    folded = unicodedata.normalize("NFKD", s.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


def lineage_supported(model: str) -> Optional[Dict[str, str]]:
    """Best-effort match of a free-text model name to a LineageOS device."""
    want = _norm(model)
    if not want:
        return None
    best = None
    for d in lineage_devices():
        full = _norm(f"{d.get('vendor','')} {d.get('name','')}")
        if not full.strip():
            continue
        # Require the device's full marketing name to appear in the query, so
        # "Galaxy S21" does not spuriously match "Galaxy S21 Ultra".
        if full and full in want:
            if best is None or len(full) > len(_norm(f"{best['vendor']} {best['name']}")):
                best = d
    return best


# --------------------------------------------------------------------------
# Specs via web search (cached per model)
# --------------------------------------------------------------------------
SPEC_PROMPT = """Look up this device and report ONLY facts you verify on the web.

Device kind: {kind}
Listing text: {model}

Reply as strict JSON, no prose, exactly these keys:
{{"model": "canonical product name",
 "kind": "phone" or "laptop",
 "chip": "exact SoC or CPU, e.g. Snapdragon 888 / Intel Core i5-1135G7",
 "benchmark_name": "antutu_v10" for phones, "passmark_cpu" for laptops,
 "benchmark_score": integer or null,
 "ram_gb": [RAM variants in GB as integers],
 "storage_gb": [storage variants in GB as integers],
 "release_year": integer or null,
 "used_price_eur": integer or null,
 "source": "where the numbers came from"}}

benchmark_score: for a phone use its AnTuTu v10 total score; for a laptop use
the PassMark CPU Mark of its processor. Use null if you cannot verify it --
never estimate. used_price_eur = typical second-hand price in Europe, EUR,
good working condition."""


# Rough class-typical ceilings used to normalise a raw benchmark to 0..1.
# Chosen as "a genuinely high-end current device", so ~1.0 means flagship and
# ~0.3 means entry level. They only need to be consistent, not exact.
# Calibrated from the downloaded tables themselves (n=5,247 phones and
# n=2,844 CPUs): the ceiling is roughly the 99th percentile, so a current
# flagship lands near 1.0 and a mid-range device sits mid-scale. Without an
# entry here a score is measured against the wrong scale entirely -- a
# PassMark phone score of 9,587 read as an AnTuTu total scores 0/100.
BENCHMARK_CEILING = {
    "passmark_device": 24_000,   # p99 of the Android device table
    "android_cpumark": 13_000,   # p99 of the Android CPU Mark chart
    # iOS CPU Mark runs far higher than the Android chart for the same class
    # of device -- Apple silicon tops the iPhone chart near 27,000 where
    # Android peaks near 19,000 -- so it needs its own ceiling or every
    # iPhone would normalize to a near-perfect score.
    "ios_cpumark": 22_000,       # p99 of the iPhone/iPad CPU Mark chart
    "passmark_gpu": 28_000,      # p99 of the GPU table
    "antutu_v10": 2_000_000,   # current flagship phones land around here
    "antutu_v9": 1_000_000,    # v9 scores run roughly half of v10
    "antutu": 1_500_000,       # version unstated - assume something between
    "passmark_cpu": 30_000,    # strong current laptop CPU
    "geekbench6_multi": 15_000,
}


# Fair-price curve, fitted to two anchors the buyer gave:
#   Galaxy S21   -- 800,000 AnTuTu, 8GB -- is worth about EUR 220 used
#   a junk phone -- 150,000 AnTuTu, 4GB -- is worth about EUR 55
# Solving a power law through those points gives the exponent and constant
# below. Laptops are fitted the same way against PassMark CPU Mark:
#   ThinkPad T480 (i5-8350U, 5,900) ~ EUR 200;  i5-13450HX (24,437) ~ EUR 700
#
# The point is to stop asking a language model what a phone is "worth". That
# guess is what let an iPhone 15 Pro at 31% off be called a great deal. A
# benchmark number and a formula cannot be talked round.
PHONE_PRICE_C, PHONE_PRICE_P = 39.3, 0.828      # EUR per (AnTuTu/100k)^p
# One curve PER BENCHMARK, rather than converting every score into an
# "AnTuTu-equivalent" and pricing that. The conversion assumed the scales are
# proportional and they are not: fitted against the same second-hand prices,
# the exponents come out 0.83 for AnTuTu, 1.30 for Android CPU Mark, 1.42 for
# the PassMark device score and 2.02 for iOS CPU Mark. Rescaling by a ratio of
# ceilings cannot reconcile curves of those shapes, and the error lands
# hardest at the ends -- exactly where "is this a deal" is decided.
#
# (C, P, divisor): value = C * (score / divisor) ** P, in EUR.
# Fitted by least squares in log space. The SCORES are from the tables; the
# prices they were fitted to are estimates of the EU second-hand market
# (typical asking prices, good condition) and are the part to revisit if the
# valuations drift -- they are judgement, not measurement.
PHONE_PRICE_CURVES = {
    # Galaxy S21 800k -> ~EUR 220 (the original fit, unchanged).
    "antutu_v10": (PHONE_PRICE_C, PHONE_PRICE_P, 1e5),
    "antutu_v9": (PHONE_PRICE_C, PHONE_PRICE_P, 1e5),
    "antutu": (PHONE_PRICE_C, PHONE_PRICE_P, 1e5),
    # Redmi Note 8 5,370->70, A32 7,337->95, S10 9,624->150, S20 9,880->170,
    # A54 12,026->200, S21 11,972->220. Residuals within 10%.
    "passmark_device": (6.13, 1.422, 1e3),
    # Redmi Note 8 2,529->70, S10 4,525->150, S21 5,741->220, A54 5,971->200.
    "android_cpumark": (21.01, 1.301, 1e3),
    # iPhone 8 4,150->70, 11 5,371->160, 13 Pro 10,003->420, 15 10,393->520,
    # 16 Pro 12,905->800. Apple holds value far better than Android, which is
    # why this exponent is the steepest of the four -- and why folding it into
    # the AnTuTu curve mispriced every iPhone.
    "ios_cpumark": (4.46, 2.021, 1e3),
}
# "Not worth buying at any price", per scale. Expressed in each benchmark's
# own units for the same reason the curves are.
PHONE_WEAK_FLOOR = {
    "antutu_v10": 300_000, "antutu_v9": 150_000, "antutu": 225_000,
    "passmark_device": 4_500, "android_cpumark": 2_200, "ios_cpumark": 3_000,
}
LAPTOP_PRICE_C, LAPTOP_PRICE_P = 41.8, 0.882    # EUR per (PassMark/1000)^p
# Desktops need their own curve: for the same CPU Mark a tower is worth far
# less than a notebook -- no screen, battery or portability, and desktop
# chips score higher per euro. Anchors (provisional): an i5-8400 tower
# (~8,000) about EUR 140, a Ryzen 5 5600 machine (~21,000) about EUR 350.
DESKTOP_PRICE_C, DESKTOP_PRICE_P = 19.4, 0.949
# Storage moves the price of a computer in a way it does not for a phone:
# drives are replaceable and separately priced, so a 1TB machine really is
# worth more than the same machine with 256GB. 256GB is the baseline; the
# exponent keeps the effect proportionate -- doubling to 512GB adds about
# 11%, 1TB about 23%, and 128GB takes off about 10% -- and it is clamped so
# a huge array cannot outweigh the processor.
STORAGE_BASELINE_GB = 256
STORAGE_EXPONENT = 0.15
STORAGE_CLAMP = (0.85, 1.35)
# Below this there is no interesting phone at any price.
MIN_USEFUL_ANTUTU = 300_000
# Buyer's bar for "a deal": price at or below this fraction of fair value,
# i.e. at least (1 - DEAL_PRICE_FRACTION) off. Kept in step with the
# graduated discount curve in config.toml's extra_prompt (currently ~40%
# off at the low end) so the arithmetic-only fast path in
# deterministic_verdict() does not apply a stricter bar than the AI path.
DEAL_PRICE_FRACTION = 0.60


def antutu_equivalent(facts: Dict[str, Any]) -> Optional[float]:
    """Put any supported benchmark on one comparable scale.

    The tables speak three dialects (AnTuTu totals, PassMark device scores,
    Android CPU Mark). Normalising each against its own ceiling and scaling
    to AnTuTu makes one price curve usable for all of them.
    """
    score = facts.get("benchmark_score")
    if not isinstance(score, (int, float)) or isinstance(score, bool) or score <= 0:
        return None
    name = (facts.get("benchmark_name") or "").lower()
    ceiling = BENCHMARK_CEILING.get(name)
    if ceiling is None:
        return None
    if name.startswith("antutu"):
        return float(score) * (BENCHMARK_CEILING["antutu_v10"] / ceiling)
    return min(score / ceiling, 1.25) * BENCHMARK_CEILING["antutu_v10"]


def fair_price(facts: Dict[str, Any]) -> Dict[str, Any]:
    """Fair used price from performance, and the price that makes it a deal.

    Returns {} when the device has no benchmark: an unverified device gets
    no computed price, rather than a made-up one.
    """
    out: Dict[str, Any] = {}
    kind = facts.get("kind")
    score = facts.get("benchmark_score")
    if not isinstance(score, (int, float)) or isinstance(score, bool) or score <= 0:
        return out

    if kind == "desktop":
        value = DESKTOP_PRICE_C * (float(score) / 1000.0) ** DESKTOP_PRICE_P
    elif kind == "laptop":
        value = LAPTOP_PRICE_C * (float(score) / 1000.0) ** LAPTOP_PRICE_P
    else:
        name = (facts.get("benchmark_name") or "").lower()
        curve = PHONE_PRICE_CURVES.get(name)
        if curve is None:
            # An unrecognised scale gets no price rather than a converted
            # guess: pricing off the wrong curve is how a flagship gets
            # called too weak to buy.
            return out
        c, p, divisor = curve
        value = c * (float(score) / divisor) ** p
        floor = PHONE_WEAK_FLOOR.get(name)
        if floor is not None and score < floor:
            out["too_weak"] = True
        # Kept for display and for the quality score, which is a RELATIVE
        # 0-100 rating where putting every device on one axis is legitimate.
        # It no longer takes any part in pricing.
        equiv = antutu_equivalent(facts)
        if equiv is not None:
            out["antutu_equivalent"] = int(equiv)

    # RAM shifts the value, but mildly: the chip dominates how a device feels.
    ram = [r for r in (facts.get("ram_gb") or [])
           if isinstance(r, (int, float)) and not isinstance(r, bool) and r > 0]
    if ram:
        value *= min(max(min(ram) / 8.0, 0.6), 1.4) ** 0.3

    # Storage counts for computers only. On a phone it is soldered in and
    # barely moves the second-hand price; on a PC or laptop the drive is a
    # separately valued component.
    if kind in ("laptop", "desktop"):
        storage = [g for g in (facts.get("storage_gb") or [])
                   if isinstance(g, (int, float)) and not isinstance(g, bool) and g > 0]
        if storage:
            mult = (min(storage) / STORAGE_BASELINE_GB) ** STORAGE_EXPONENT
            mult = min(max(mult, STORAGE_CLAMP[0]), STORAGE_CLAMP[1])
            value *= mult
            out["storage_factor"] = round(mult, 3)

    out["fair_price_eur"] = int(round(value))
    out["deal_price_eur"] = int(round(value * DEAL_PRICE_FRACTION))
    return out


def quality_score(facts: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic good/bad verdict from chip + RAM + storage.

    The LLM supplies the raw numbers (cached per model); the judgement is made
    here in plain arithmetic so it is reproducible and cannot be talked into a
    different answer by a persuasive listing.

    Two rules learned from live data:

    * The chip is mandatory for a confident verdict. Without a benchmark the
      score is capped and marked low-confidence -- otherwise a device with no
      known CPU but generous RAM/storage options scored "great", which is
      how a ThinkPad with no benchmark once came out at 125/100.
    * RAM/storage use the SMALLEST advertised variant. These lists describe
      configurations the model was sold in, not the unit in the listing, so
      the smallest is the only figure that cannot overstate what is on offer.
    """
    bench = facts.get("benchmark_score")
    name = (facts.get("benchmark_name") or "").lower()
    ceiling = BENCHMARK_CEILING.get(name)
    if ceiling is None:
        ceiling = BENCHMARK_CEILING[
            "passmark_cpu" if facts.get("kind") in ("laptop", "desktop") else "antutu_v10"
        ]

    parts: Dict[str, Any] = {}
    if isinstance(bench, (int, float)) and not isinstance(bench, bool) and bench > 0:
        parts["chip"] = min(bench / ceiling, 1.0)

    def _smallest(key: str) -> Optional[float]:
        vals = [
            v for v in (facts.get(key) or [])
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
        ]
        return min(vals) if vals else None

    ram = _smallest("ram_gb")
    if ram is not None:
        parts["ram"] = min(ram / 8.0, 1.0)      # 8GB is "fine"
    store = _smallest("storage_gb")
    if store is not None:
        parts["storage"] = min(store / 256.0, 1.0)

    if not parts:
        return {"score": None, "verdict": "unknown", "confidence": "none", "parts": {}}

    weights = {"chip": 0.70, "ram": 0.25, "storage": 0.05}
    used = {k: v for k, v in parts.items() if k in weights}
    total_w = sum(weights[k] for k in used) or 1.0
    score = 100 * sum(parts[k] * weights[k] for k in used) / total_w

    if "chip" in parts:
        confidence = "high"
    else:
        # No benchmark: RAM/storage alone cannot justify calling it good.
        confidence = "low"
        score = min(score, 45)

    score = int(round(max(0.0, min(100.0, score))))
    verdict = "great" if score >= 75 else "good" if score >= 55 else "ok" if score >= 35 else "weak"
    if confidence == "low":
        verdict += " (no benchmark)"
    return {
        "score": score,
        "verdict": verdict,
        "confidence": confidence,
        "parts": {k: round(v, 2) for k, v in parts.items()},
    }


def fetch_specs(model: str, api_key: str, kind: str = "auto", timeout: int = 45) -> Dict[str, Any]:
    """Ask a web-searching model for this device's specs, as JSON.

    Failures split into two kinds:

    * busy (HTTP 413/429) -- Groq answers 413, not 429, when a request would
      exceed the remaining tokens-per-minute allowance, and this helper shares
      that budget with the monitor service. Worth one short wait, then the
      next (lighter) model. Surfaced as RateLimited so callers can tell
      "try later" from "broken".
    * fatal (bad request, auth, server error, DNS) -- another model would fail
      identically, so stop immediately rather than burning further requests.
    """
    last_err: Optional[Exception] = None

    # Providers are tried in order and each carries its OWN daily/per-minute
    # allowance, so a second one is not redundancy -- it is the difference
    # between the spec queue draining and sitting at zero. Groq's budget is
    # shared with listing evaluation, and when it runs out BOTH stop.
    endpoints = ([(GROQ_URL, api_key, m)
                  for m in GROQ_SEARCH_MODELS + GROQ_FALLBACK_MODELS]
                 if api_key and api_key != "-" else [])
    cerebras_key = os.environ.get("CEREBRAS_API_KEY", "")
    if cerebras_key:
        endpoints += [(CEREBRAS_URL, cerebras_key, m) for m in CEREBRAS_MODELS]
    endpoints = [e for e in endpoints if e[0] not in _DISABLED_ENDPOINTS]

    for url, key, model_name in endpoints:
        for attempt in range(2):
            payload = {
                "model": model_name,
                "messages": [
                    {"role": "user", "content": SPEC_PROMPT.format(model=model, kind=kind)}
                ],
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "User-Agent": UA,
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    body = json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code in (413, 429):
                    last_err = RateLimited(f"{model_name}: {e}")
                    if attempt == 0:
                        time.sleep(20)
                        continue
                    break  # move on to the next model
                # Anything else -- a 400 from a model that dislikes the
                # prompt, a 402 from a provider with no quota, a 503 -- is
                # this ENDPOINT's problem, not the device's. Raising here
                # abandoned every remaining model: one 400 from the first
                # Groq model meant Cerebras was never asked, and it had the
                # right answer. Remember it and carry on down the list.
                last_err = e
                if e.code == 402:
                    _DISABLED_ENDPOINTS.add(url)
                break
            except urllib.error.URLError as e:
                last_err = RuntimeError(f"network error contacting {url}: {e}")
                break

            content = body["choices"][0]["message"].get("content") or ""
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if not m:
                raise ValueError(f"no JSON in response from {model_name}: {content[:150]}")
            facts = json.loads(m.group(0))
            # A model that answers "{chip: null, ram_gb: null, ...}" has
            # declined, and returning that ended the chain: for an Honor X8
            # the first Groq model answered all-nulls and Cerebras, which
            # names the chip correctly, was never asked. An empty answer is a
            # miss, not a result.
            if not any(facts.get(k) for k in ("chip", "benchmark_score", "ram_gb")):
                last_err = last_err or ValueError(f"{model_name} returned no specs")
                break                       # next model
            facts["looked_up_by"] = model_name
            if model_name in GROQ_FALLBACK_MODELS + CEREBRAS_MODELS:
                # No web access: recalled, not verified. Recorded so the
                # figures can be told apart from searched ones later.
                facts["unverified"] = True
            return facts

    raise last_err or RuntimeError("spec lookup failed")


def _record_to_database(facts: Dict[str, Any], kind: str) -> None:
    """Write a web-looked-up benchmark into the local database.

    The point is that it is looked up ONCE, ever: the next listing for the
    same phone -- and for anyone else running this -- is answered offline
    from the table. Values are range-checked first, and anything from a
    non-searching fallback model is refused, since those answer from
    memory rather than from a source and are not worth freezing into a
    database.
    """
    try:
        from . import benchmark_db as bdb
    except ImportError:  # run as a standalone file, not a package member
        import benchmark_db as bdb

    score = facts.get("benchmark_score")
    name = facts.get("model") or facts.get("query")
    if not score or not name or facts.get("unverified"):
        return
    bench = (facts.get("benchmark_name") or "").lower()
    table = ("cpu" if "passmark_cpu" in bench
             else "device" if "passmark_device" in bench
             else "antutu" if "antutu" in bench
             else None)
    if table is None:
        return
    if bdb.add_entry(table, str(name), int(score), source=facts.get("looked_up_by", "web")):
        facts["stored_in_db"] = True


def soc_score(chip: str, kind: str = "phone") -> Optional[int]:
    """Curated benchmark for a NAMED chip, or None.

    Sellers and models spell a chip a dozen ways ("Qualcomm Snapdragon 8 Gen
    3", "MediaTek Dimensity 8300-Ultra", "Apple A17 Pro"), so the table name
    is matched as a phrase inside the given string and the LONGEST match wins
    -- otherwise "snapdragon 8" would answer for "snapdragon 8 elite".
    """
    if not chip:
        return None
    table = ("cpu_passmark" if kind in ("laptop", "desktop") else "soc_antutu_v10")
    hay = f" {_norm(chip)} "
    best: Optional[tuple] = None
    for name, score in (_load_chips().get(table) or {}).items():
        needle = _norm(name)
        if needle and f" {needle} " in hay or (needle and hay.strip().startswith(needle + " ")) \
                or (needle and hay.strip().endswith(" " + needle)) or needle == hay.strip():
            if best is None or len(needle) > len(best[0]):
                best = (needle, score)
    if best is None:
        _queue_soc(chip, kind)
    return best[1] if best else None


def _queue_soc(chip: str, kind: str) -> None:
    """Record a chip we have no score for, to be resolved out of band."""
    key = _norm(chip)
    # A chip name is short and specific; anything long is a device name or
    # prose that wandered in, and scoring it would poison the table.
    if not key or len(key.split()) > 5 or not any(c.isdigit() for c in key):
        return
    try:
        pending = _load(SOC_PENDING)
        if key not in pending:
            pending[key] = {"chip": chip.strip(), "kind": kind,
                            "seen_at": int(time.time())}
            _save(SOC_PENDING, pending)
    except OSError:
        pass


# Two independent answers must land within this of each other to be believed.
# Benchmark figures vary between runs and revisions, so exact agreement is not
# the bar; agreeing to within a fifth is, because an invented number and a
# recalled one do not land that close by chance.
SOC_AGREEMENT = 0.20
# When both answers come from the SAME provider they are two versions of one
# lab's weights rather than truly independent, so they have to agree more
# closely before the number is believed. Without this tier nothing fills at
# all whenever only one provider has quota left, which is most of the time.
SOC_AGREEMENT_SAME_PROVIDER = 0.10


def fill_socs(limit: int = 8, verbose: bool = True) -> int:
    """Resolve queued SoC scores, keeping only cross-validated answers.

    No free bulk source of SoC benchmarks exists that is not behind a bot
    check (kimovil, Geekbench, nanoreview and TechPowerUp were all tried), so
    the numbers have to come from models. A single model's recollection is
    exactly what this whole module distrusts -- one confidently returned the
    wrong chip entirely for a Xiaomi 14T -- so a score is written only when
    two INDEPENDENT models agree to within SOC_AGREEMENT. Disagreement means
    at least one is guessing, and the chip stays queued rather than being
    recorded wrongly.
    """
    pending = _load(SOC_PENDING)
    if not pending:
        return 0
    api_key = os.environ.get("GROQ_API_KEY", "")
    cerebras = os.environ.get("CEREBRAS_API_KEY", "")
    if not api_key and not cerebras:
        if verbose:
            print("  no API key; cannot resolve SoC scores")
        return 0

    learned = _load(SOC_LEARNED)
    done = 0
    # Chips seen in a REAL listing first (seen_at set), bulk-seeded names
    # after. A queue seeded from a vendor's full history starts at its oldest
    # parts -- Snapdragon 200/208/210, none of which any model will score and
    # none of which clears the usable-performance floor anyway -- and those
    # would otherwise absorb every round forever.
    order = sorted(pending.items(),
                   key=lambda kv: (not kv[1].get("seen_at"), kv[1].get("attempts", 0)))
    for key, info in order[:limit]:
        chip, kind = info.get("chip", key), info.get("kind", "phone")
        table = "cpu_passmark" if kind in ("laptop", "desktop") else "soc_antutu_v10"
        answers, busy, used, reachable = [], False, set(), False
        for pass_no in (1, 2):
          # Pass 1 takes at most one answer per provider, so a second opinion
          # is independent. Pass 2 runs only if that could not find two, and
          # allows a second model from a provider already used -- at the
          # tighter tolerance below.
          if pass_no == 2 and len(answers) >= 2:
              break
          for url, akey, model_name in _soc_endpoints(api_key, cerebras):
            if pass_no == 1 and url in used:
                continue
            if pass_no == 2 and any(m == model_name for m, _ in answers):
                continue
            try:
                value = _ask_soc(url, akey, model_name, chip, table)
            except RateLimited:
                # One exhausted model is not the end of the round: the whole
                # point of listing several providers is that their quotas are
                # separate, so move to the next rather than abandoning the
                # chip on the first 429.
                busy = True
                continue
            except Exception:  # noqa: BLE001 - try the next model
                continue
            reachable = True          # it replied; it just may not have committed
            if value:
                answers.append((model_name, value))
                used.add(url)
            if len(answers) == 2:
                break
        if len(answers) < 2:
            # A model declining to guess is a different thing from a model
            # being unreachable, and only the first is the chip's fault.
            if answers or reachable:
                info["attempts"] = int(info.get("attempts", 0)) + 1
                if info["attempts"] >= MAX_WARM_ATTEMPTS:
                    pending.pop(key, None)
                    if verbose:
                        print(f"  {chip}: no model will commit to a figure, retired")
                    continue
            if verbose:
                got = (f"only {answers[0][1]:,} from {answers[0][0]}" if answers
                       else ("every provider busy" if busy else "no model would commit"))
                print(f"  {chip}: {got}, left queued")
            continue
        (m1, v1), (m2, v2) = answers[0], answers[1]
        cross = len(used) > 1
        tolerance = SOC_AGREEMENT if cross else SOC_AGREEMENT_SAME_PROVIDER
        if abs(v1 - v2) > tolerance * max(v1, v2):
            if verbose:
                print(f"  {chip}: {m1} said {v1:,}, {m2} said {v2:,} -- disagree, left queued")
            continue
        score = int(round((v1 + v2) / 2))
        learned.setdefault(table, {})[_norm(chip)] = score
        pending.pop(key, None)
        done += 1
        if verbose:
            print(f"  {chip}: {score:,} ({m1} {v1:,} / {m2} {v2:,}"
                  f"{'' if cross else ', same provider'})")
    _save(SOC_LEARNED, learned)
    _save(SOC_PENDING, pending)
    _CHIPS_CACHE.clear()          # force a reload so the new scores are live
    return done


def _soc_endpoints(groq_key: str, cerebras_key: str):
    """Model endpoints for SoC scoring, most independent pairing first.

    Two models from DIFFERENT providers are preferred: they are different
    weights trained by different people, so agreement means more than two
    Groq models echoing each other.
    """
    out = []
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if gemini_key:
        out += [(GEMINI_OPENAI_URL, gemini_key, m) for m in GEMINI_MODELS]
    if cerebras_key:
        out += [(CEREBRAS_URL, cerebras_key, m) for m in CEREBRAS_MODELS]
    if groq_key:
        # Two different Groq families (gpt-oss and qwen) still count as two
        # independent recollections, so cross-checking survives Cerebras being
        # unavailable -- it just loses the different-provider guarantee.
        out += [(GROQ_URL, groq_key, m) for m in GROQ_FALLBACK_MODELS]
    return [e for e in out if e[0] not in _DISABLED_ENDPOINTS]


def _ask_soc(url: str, key: str, model_name: str, chip: str, table: str) -> Optional[int]:
    """One model's number for one chip, or None if it will not commit."""
    metric = ("PassMark CPU Mark" if table == "cpu_passmark"
              else "AnTuTu v10 total score")
    prompt = (
        f"What is the typical {metric} for the {chip}?\n"
        "Answer with ONLY a JSON object: {\"score\": <integer>} .\n"
        "If you are not confident of the figure, answer {\"score\": null} "
        "instead of estimating."
    )
    payload = {"model": model_name,
               "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (413, 429):
            raise RateLimited(f"{model_name}: {e}") from e
        if e.code == 402:
            _DISABLED_ENDPOINTS.add(url)
        raise
    content = body["choices"][0]["message"].get("content") or ""
    # Some models emit the object and then keep talking, so the greedy span
    # from the first "{" to the last "}" is not valid JSON. Try that first,
    # then the first self-contained object.
    value = None
    for pattern in (r"\{.*\}", r"\{[^{}]*\}"):
        m = re.search(pattern, content, re.DOTALL)
        if not m:
            continue
        try:
            value = json.loads(m.group(0)).get("score")
            break
        except ValueError:
            continue
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    lo, hi = ((100, 200_000) if table == "cpu_passmark" else (20_000, 4_000_000))
    return int(value) if lo <= value <= hi else None


def has_real_facts(facts: Dict[str, Any]) -> bool:
    """True when an entry holds actual looked-up data, not just a failed try.

    An offline call can only report LineageOS support, so caching its result
    as if the model were resolved makes every later call a cache hit and the
    real lookup never happens.

    An UNVERIFIED entry with no benchmark is not "resolved" either. Those come
    from the non-searching fallback models, which answer from memory: asked
    about a Xiaomi 14T one returned "Snapdragon 8 Gen 3" (that is the Xiaomi
    14; the 14T is a Dimensity 8300-Ultra) and no score at all. Counting that
    as facts is doubly wrong -- it can compute no price, so the listing still
    goes to the model, and it hands the model a made-up chip labelled as
    verified. It stays unresolved so a searching lookup can still fix it.
    """
    if facts.get("overridden") or facts.get("benchmark_score"):
        return True
    if facts.get("unverified"):
        return False
    return bool(facts.get("chip") or facts.get("ram_gb"))


def facts_from_tables(title: str, kind: str, text: str = "") -> Dict[str, Any]:
    """Specs from downloaded/shipped data. No network call, no AI.

    Order of preference:
      1. PassMark's own tables -- ~2,800 CPUs and ~5,200 Android phones,
         downloaded once. Authoritative and complete for laptops.
      2. A shipped model -> chip -> score table, for the phones PassMark's
         submission-driven device list happens not to carry (it has no
         Galaxy S20, Redmi Note 12 or Honor 8X, for instance).
    """
    try:
        from . import benchmark_db as bdb
    except ImportError:  # run as a standalone file, not a package member
        import benchmark_db as bdb

    # Sellers often leave the model out of the title and spell it in the
    # description ("Samsung" / "...je to Galaxy S21 5G, 128GB..."). Searching
    # the full text finds those; it costs a dict lookup per n-gram.
    haystack = f"{title} {text}".strip() if text else title

    out: Dict[str, Any] = {}
    if kind in ("laptop", "desktop"):
        hit = bdb.lookup_cpu(haystack)
        if hit:
            out.update(chip=hit[0], benchmark_name="passmark_cpu", benchmark_score=hit[1])
    else:
        hit = bdb.lookup_device(haystack)
        if hit:
            out.update(chip=hit[0], benchmark_name="passmark_device", benchmark_score=hit[1])
        elif bdb.lookup_device_cpu(haystack):
            # Second phone table: different coverage, CPU Mark rather than the
            # overall score, so it has its own scale.
            hit = bdb.lookup_device_cpu(haystack)
            out.update(chip=hit[0], benchmark_name="android_cpumark", benchmark_score=hit[1])
        elif bdb.lookup_device_ios(haystack):
            # iPhones and iPads appear in none of the Android tables.
            hit = bdb.lookup_device_ios(haystack)
            out.update(chip=hit[0], benchmark_name="ios_cpumark", benchmark_score=hit[1])
        else:
            # Entries learned from earlier web lookups, stored as AnTuTu.
            learned = bdb.match_device(haystack, bdb.load(auto_download=False).get("antutu", {}))
            if learned:
                out.update(chip=learned[0], benchmark_name="antutu_v10",
                           benchmark_score=learned[1])

    if not out:
        chips = _load_chips()
        key = model_key(title)
        entry = None
        for name, info in chips.get("models", {}).items():
            if override_applies(key, model_key(name)):
                if entry is None or len(name) > len(entry[0]):
                    entry = (name, info)
        if entry:
            info = entry[1]
            chip = info.get("chip", "")
            out.update(chip=chip, ram_gb=info.get("ram_gb"),
                       release_year=info.get("release_year"))
            table = ("cpu_passmark" if kind in ("laptop", "desktop")
                     else "soc_antutu_v10")
            score = chips.get(table, {}).get(chip)
            if score:
                out["benchmark_name"] = ("passmark_cpu"
                                         if kind in ("laptop", "desktop")
                                         else "antutu_v10")
                out["benchmark_score"] = score
    if out:
        out["source"] = "benchmark tables (no AI)"
    return out


_CHIPS_CACHE: Dict[str, Any] = {}


def _load_chips() -> Dict[str, Any]:
    if not _CHIPS_CACHE:
        try:
            path = Path(__file__).with_name("chips.json")
            _CHIPS_CACHE.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            _CHIPS_CACHE["models"] = {}
        # Chips learned since. Kept OUTSIDE the package because chips.json is
        # shipped data and a reinstall replaces it; this file survives, the
        # way benchmarks_local.json does for the device tables. The shipped
        # table covers ~86 SoCs, which is a fraction of what turns up in
        # listings, so this is where the rest accumulates.
        try:
            extra = json.loads(SOC_LEARNED.read_text(encoding="utf-8"))
            for table, rows in extra.items():
                if isinstance(rows, dict):
                    _CHIPS_CACHE.setdefault(table, {}).update(rows)
        except (OSError, ValueError):
            pass
    return _CHIPS_CACHE


def device_facts(model: str, refresh: bool = False, offline: bool = False,
                 kind: str = "auto", text: str = "") -> Dict[str, Any]:
    """Facts for one model. Cached, so each model costs one lookup ever.

    offline=True skips the (slow, rate-limited) web spec lookup and returns
    whatever is cached plus LineageOS support, which is a fast local check.
    Use that on any hot path; warm the cache separately.
    """
    cache = _load(FACTS_CACHE)
    key = model_key(model)
    if not is_identifiable(key):
        # Brand without model. No table, override or lookup can honestly
        # answer THE TITLE, and every one of them used to answer anyway.
        # The description may still name the device, so search that; a hit
        # there is an exact known product name, not a guess.
        kind = kind if kind != "auto" else guess_kind(model)
        facts = {"query": model, "kind": kind}
        facts.update(facts_from_tables(model, kind, text) if text else {})
        if not has_real_facts(facts):
            facts["specs_error"] = "no model in title"
            return facts
        # Deliberately not cached: the key ("samsung") names no device, so
        # storing it here is what poisoned the cache in the first place.
        facts.update(quality_score(facts))
        facts.update(fair_price(facts))
        return facts

    if not refresh and key in cache:
        return cache[key]

    if kind == "auto":
        kind = guess_kind(model)
    facts: Dict[str, Any] = {"query": model, "kind": kind}
    # Free, instant, deterministic. Only fall through to a web lookup for
    # models no table knows about.
    facts.update(facts_from_tables(model, kind, text))

    # Either provider is enough to attempt a lookup. Gating on GROQ_API_KEY
    # alone meant a Cerebras-only setup silently never looked anything up.
    have_key = bool(os.environ.get("GROQ_API_KEY") or os.environ.get("CEREBRAS_API_KEY"))
    api_key = "" if offline or has_real_facts(facts) or not have_key \
        else os.environ.get("GROQ_API_KEY", "-")
    if api_key:
        try:
            looked_up = fetch_specs(model, api_key, kind=kind)
            facts.update(looked_up)
            _record_to_database(facts, kind)
        except Exception as e:  # network/quota/parse - degrade, never crash
            facts["specs_error"] = str(e)[:200]
            # Swallowing the exception keeps every caller crash-free, but it
            # also erased the one distinction that matters to the queue:
            # "the provider is busy, ask again later" versus "this device
            # cannot be resolved". warm_cache read both as busy and stopped
            # the batch, so one unresolvable entry at the head of the queue
            # blocked everything behind it indefinitely. Record the kind.
            facts["specs_busy"] = isinstance(e, RateLimited)
    elif not has_real_facts(facts):
        facts["specs_error"] = "offline" if offline else "GROQ_API_KEY not set"

    # SoC-first scoring. A model is reliable at naming the chip in a phone and
    # unreliable at recalling its benchmark: asked about a Xiaomi 14T one
    # returned a plausible chip with no score at all, and another returned a
    # confident score for the wrong chip. chips.json already carries a curated
    # AnTuTu figure for ~86 SoCs, so whenever the chip is known and the score
    # is not, take the number from the table rather than from the model. This
    # is also what rescues devices no device-level table lists -- there are no
    # Redmi Note 12/13/14 rows anywhere, but their SoCs are all in the table.
    # An UNVERIFIED score is one model's recollection. A SoC-table score was
    # either shipped or agreed on by two independent models, so it wins:
    # asked about a Redmi Note 14 a model volunteered 365,000 for a Dimensity
    # 7025 Ultra, where the cross-checked table says 502,500. Prefer the
    # table whenever it knows the chip.
    if (facts.get("unverified") and facts.get("chip")
            and soc_score(str(facts["chip"]), kind=facts.get("kind", kind))):
        facts.pop("benchmark_score", None)
    if not facts.get("benchmark_score") and facts.get("chip"):
        score = soc_score(str(facts["chip"]), kind=facts.get("kind", kind))
        if score:
            facts["benchmark_name"] = ("passmark_cpu"
                                       if facts.get("kind", kind) in ("laptop", "desktop")
                                       else "antutu_v10")
            facts["benchmark_score"] = score
            facts["score_from_soc_table"] = True
            # The number is now ours, not the model's recollection, so the
            # entry is no longer only-as-good-as-an-unverified-guess.
            facts.pop("unverified", None)

    lo = lineage_supported(model)
    facts["lineageos_official"] = bool(lo)
    if lo:
        facts["lineageos_device"] = f"{lo['vendor']} {lo['name']} ({lo['codename']})".strip()

    # Manual overrides win over everything above.
    for ov_key, ov in _load(OVERRIDES).items():
        # Normalize the override key too, so an entry written naturally
        # ("iPhone 11 phone") still matches a key that has had filler words
        # stripped. Identity rules apply: an override for the S21 must not
        # bleed onto the S21 Ultra, nor onto a bare "Samsung".
        ov_norm = model_key(ov_key)
        if ov_norm and override_applies(key, ov_norm):
            facts.update(ov)
            facts["overridden"] = True

    facts.update(quality_score(facts))
    facts.update(fair_price(facts))
    facts["cached_at"] = int(time.time())
    # Only persist a resolved entry. Storing an offline/failed attempt would
    # mask the model as "known" and stop it ever being looked up properly.
    if has_real_facts(facts):
        cache[key] = facts
        _save(FACTS_CACHE, cache)
    return facts


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
# Only phones, tablets and laptops have a benchmark this tool can use. The
# monitor also surfaces furniture, clothing, consoles, TVs and accessories,
# and every one of those queued for a lookup wastes a rate-limited web search
# on a device that has no SoC or CPU score at all.
#
# Order matters: accessories are checked FIRST, because they almost always
# name the device they attach to ("Podstavec pre notebook", "Asus Notebook
# SSD disk", "Sieťový adaptér Asus ROG"), and a brand match alone would
# happily queue a laptop stand.

# Things sold FOR a device, not the device. Slovak and English.
ACCESSORY_MARKERS = (
    "podstavec", "stojan", "stand", "adapter", "adaptér", "nabijacka", "nabíjačka",
    "charger", "kabel", "kábel", "cable", "puzdro", "obal", "kryt", "case", "cover",
    "sklo", "folia", "fólia", "screen protector", "dokovacia", "dokovaci",
    "docking", "dock", "klavesnica", "klávesnica", "keyboard", "mys", "myš",
    "mouse", "pencil", "stylus", "pero", "remienok", "strap", "sluchadla",
    "slúchadlá", "headphones", "earbuds", "buds", "taska", "taška", "batoh",
    # "hdd" alone matched the spec list of a real laptop ("512gb SSD 1TB HDD"),
    # so it must name the product, not appear among its specs.
    "backpack", "sleeve", "ssd disk", "hdd disk", "externy hdd", "external hdd",
    "ram modul", "pamet", "pamäť",
    "chladic", "chladič", "cooler", "webcam", "hub", "redukcia", "napajaci",
    "napájací", "power supply", "diely", "nahradne", "náhradné",
    "galaxy ring", "smart ring", "smartwatch", "galaxy fit", "mi band",
)

# Devices with no comparable CPU/SoC benchmark in this scheme.
NON_COMPUTE_MARKERS = (
    "playstation", "ps3", "ps4", "ps5", "xbox", "nintendo", "switch", "konzola",
    "console", "televizor", "televízor", " tv ", "tv ", "smart tv", "monitor",
    "projektor", "reproduktor", "speaker", "pracka", "práčka", "susicka",
    "sušička", "chladnicka", "chladnička", "mraznicka", "mraznička", "bicykel",
    "gitara", "hodinky", "watch", "band", "kamera", "fotoaparat", "dron",
    # Displays are often named only by product line and refresh rate
    # ("MSI Optix G27C6 - 27\u201d, 165 Hz"), never the word "monitor".
    " hz", "optix", "odyssey g", "ultragear", "nitro xv", "viewsonic",
    # A graphics tablet is not a compute tablet.
    "wacom", "graficky tablet", "grafický tablet", "intuos", "huion",
)

DESKTOP_HINTS = (
    "stolny pocitac", "stolovy pocitac", "desktop", "zostava", "skrinka",
    "herny pc", "herné pc", "gaming pc", "pc zostava", "tower", "imac",
    "mac mini", "mac studio", "workstation", "staci pc", "starsi pc",
    " pc ", "pocitac",
)

LAPTOP_HINTS = (
    "laptop", "notebook", "macbook", "thinkpad", "ideapad", "vivobook", "zenbook",
    "latitude", "elitebook", "probook", "inspiron", "pavilion", "chromebook",
    "ultrabook", "aspire", "nitro", "legion", "omen", "predator", "yoga",
    "surface laptop", "zephyrus", "alienware", "loq", "tuf", "rog",
)

# Phones and tablets alike run a mobile SoC with an AnTuTu score.
MOBILE_HINTS = (
    "iphone", "ipad", "galaxy", "samsung a", "samsung s", "redmi", "poco",
    "xiaomi", "huawei", "honor", "realme", "oneplus", "oppo", "vivo", "pixel",
    "motorola", "moto g", "nokia", "xperia", "nothing phone", "telefon",
    "telefón", "mobil", "smartfon", "smartfón", "tablet", "tab s",
)

COMPUTE_BRANDS = (
    "samsung", "apple", "xiaomi", "huawei", "honor", "realme", "oneplus", "oppo",
    "vivo", "google", "motorola", "nokia", "sony", "nothing", "asus", "lenovo",
    "hp", "dell", "acer", "msi", "microsoft", "alienware", "gigabyte", "razer",
)


def _has(text: str, markers) -> bool:
    return any(m in text for m in markers)


def looks_like_accessory(title: str) -> bool:
    """True if the listing IS an accessory, not merely one that mentions one.

    The distinction is the whole point. A plain substring test cannot make it:
    "nabíjačka" and "kábel" appear in the description of most complete phones
    for sale, so testing for them rejected the phones they came with. What
    marks an accessory is the accessory word naming the PRODUCT -- which is
    what the head of the title says, before the extras.
    """
    # Judged on the part BEFORE a "+", because a bundle lists the device first
    # and the extras after it: "MacBook Air + Magic Mouse" is a laptop, while
    # "Asus Notebook SSD disk" and "Podstavec pre notebook" are an SSD and a
    # stand that merely name what they attach to. Split the RAW title: _norm
    # strips punctuation, so splitting afterwards would never find "+".
    # A bundle also lists its extras after "with" ("s klávesnicou, perom" -- a
    # TABLET sold with a keyboard and pen, not a pen), so that clause is cut
    # the same way.
    primary = " " + _norm(title.split("+")[0]) + " "
    primary = re.split(r"\s(?:s|so|vratane|with|incl|including)\s", primary)[0] + " "
    if _has(primary, ACCESSORY_MARKERS):
        return True

    # "<something> pre iPad Pro" / "<something> for iPhone" names an accessory
    # by what it ATTACHES TO. When the only device word in the title sits
    # after that "for", the thing being sold is not the device -- a "Logitech
    # Combo Touch pre iPad Pro" is a keyboard case, and no marker list will
    # ever contain every accessory's brand name. Titles that name the device
    # first ("iPad Pro 11 na predaj") are unaffected, because the hint is
    # still present in the head.
    head = re.split(r"\s(?:pre|for|na)\s", primary)[0] + " "
    hints = LAPTOP_HINTS + DESKTOP_HINTS + MOBILE_HINTS
    return head != primary and not _has(head, hints) and _has(primary, hints)


def classify(title: str) -> str:
    """Return 'phone', 'laptop' or 'other' for a listing title.

    'other' means: do not spend a spec lookup on this.
    """
    t = " " + _norm(title) + " "

    if looks_like_accessory(title):
        return "other"

    # Laptops first: gaming models legitimately mention refresh rates ("144Hz")
    # that would otherwise read as a monitor. Laptop before desktop too, since
    # "herny notebook" contains neither ambiguity but "PC" appears in plenty
    # of laptop ads.
    if _has(t, LAPTOP_HINTS):
        return "laptop"
    if _has(t, DESKTOP_HINTS):
        return "desktop"
    if _has(t, NON_COMPUTE_MARKERS):
        return "other"
    if _has(t, MOBILE_HINTS):
        return "phone"
    # Deliberately no "known brand + a digit" fallback: it classified an
    # "MSI Optix G27C6" monitor as a phone. Skipping an unrecognised device
    # costs one missing spec line; guessing wastes a rate-limited lookup and
    # produces a nonsense score.
    return "other"


def guess_kind(text: str) -> str:
    """Device kind for spec lookup; defaults to phone for unknown devices."""
    kind = classify(text)
    return kind if kind in ("laptop", "desktop") else "phone"


def looks_benchmarkable(title: str) -> bool:
    """Whether a listing is a phone/tablet/laptop worth resolving specs for."""
    return classify(title) != "other"


def parse_price(text: Any) -> Optional[float]:
    """Pull a number out of a marketplace price string.

    Handles "EUR 190", "190 EUR", "190,50", "1 200 EUR" and the ranges
    Facebook sometimes renders ("40 | 70" -> take the lower, which is what
    the buyer would pay).
    """
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    if not isinstance(text, str):
        return None
    t = text.replace("\u00a0", " ")
    if re.search(r"\b(free|zadarmo|zdarma)\b", t, re.I):
        return 0.0
    nums = []
    for raw in re.findall(r"\d[\d \u00a0.,]*", t):
        cleaned = raw.replace(" ", "").replace("\u00a0", "")
        # 1.299,50 and 1,299.50 both mean the same amount
        if "," in cleaned and "." in cleaned:
            cleaned = (cleaned.replace(".", "").replace(",", ".")
                       if cleaned.rfind(",") > cleaned.rfind(".")
                       else cleaned.replace(",", ""))
        elif "," in cleaned:
            cleaned = cleaned.replace(",", "." if len(cleaned.split(",")[-1]) == 2 else "")
        else:
            cleaned = cleaned.replace(".", "") if cleaned.count(".") == 1 and len(cleaned.split(".")[-1]) == 3 else cleaned
        try:
            nums.append(float(cleaned))
        except ValueError:
            continue
    nums = [n for n in nums if n >= 0]
    return min(nums) if nums else None


# Words that say the thing does not work. Benchmark data describes a working
# device, so the arithmetic values a cracked phone exactly like a good one --
# it priced "Samsung A51 na opravu, alebo nahradne diely" as an 87%-off deal.
# Condition is the model's job, so these listings are handed over rather than
# settled here.
DAMAGE_MARKERS = (
    "na diely", "nahradne diely", "náhradné diely", "na opravu", "na sucastky",
    "na súčiastky", "rozbity", "rozbitý", "rozbite", "rozbité", "prasknuty",
    "prasknutý", "prasknute", "prasknuté", "puknuty", "puknutý", "nefunkcny",
    "nefunkčný", "nefunkcna", "nefunkčná", "nefunkcne", "nefunkčné", "pokazeny",
    "pokazený", "poskodeny", "poškodený", "poskodene", "poškodené", "vadny",
    "vadný", "zablokovany", "zablokovaný", "icloud lock", "for parts",
    "spare parts", "broken", "cracked", "faulty", "not working", "defective",
    "cez displej", "praskly displej", "prasknuty displej", "nejde zapnut",
    "nezapina", "nezapína", "nabija len", "bez zaruky na funkcnost",
)


# A genuine bargain is 50-70% off. It is not 95% off. Below this fraction of
# working value the listing is bait, not a find: eight "RTX 4080 gaming
# laptops" at EUR 109.99 were pushed straight past the model's scam checks
# because the arithmetic loved them. Too-good-to-be-true is exactly the case
# that needs a reader, so it goes to the model rather than being dropped --
# it can still rate it a deal if the thing is real.
TOO_GOOD_FRACTION = 0.15

SELLER_STATS = CACHE_DIR / "seller_stats.json"

# One seller listing this many items at an identical price is running a shop,
# not clearing a drawer. The scam that prompted this had every item -- laptops,
# a fridge, a television -- at exactly EUR 109.99.
BULK_SELLER_LISTINGS = 3


def note_seller(seller: str, price_text: Any) -> None:
    """Record that a seller listed something at a price."""
    price = parse_price(price_text)
    if not seller or price is None:
        return
    try:
        stats = _load(SELLER_STATS)
        by_price = stats.setdefault(seller.strip().lower(), {})
        bucket = f"{price:.2f}"
        if bucket not in by_price:
            by_price[bucket] = 0
        by_price[bucket] += 1
        _save(SELLER_STATS, stats)
    except OSError:
        pass


def seller_looks_bulk(seller: str, price_text: Any) -> bool:
    """True if this seller has posted several items at this exact price."""
    price = parse_price(price_text)
    if not seller or price is None:
        return False
    stats = _load(SELLER_STATS)
    count = stats.get(seller.strip().lower(), {}).get(f"{price:.2f}", 0)
    return count >= BULK_SELLER_LISTINGS


# What a broken device is worth: parts money, not a discount off retail.
DAMAGED_PRICE_FRACTION = 0.20


def looks_damaged(text: str) -> bool:
    """True if the listing says the device is broken, locked or for parts."""
    return any(m in _norm(text) or m in text.lower() for m in DAMAGE_MARKERS)


def deterministic_verdict(
    title: str, price_text: Any, kind: str = "auto", text: str = "",
    seller: str = "",
) -> Optional[Dict[str, Any]]:
    """Settle a listing from data alone, or hand it to the model.

    Returns one of:
      {"decision": "deal",   ...}  price is at or below the computed bar
      {"decision": "reject", ...}  specs are known and it is not cheap enough
      None                         not enough information; the model decides

    Both outcomes are final when the numbers are complete. Sending a
    known-and-not-cheap-enough listing to the model wastes a rate-limited
    request on a question arithmetic already answered -- and invites it to
    talk itself into a "deal" on specs, which is the failure this whole
    path exists to prevent.
    """
    damaged = looks_damaged(f"{title} {text}")

    # An accessory is settled here rather than by the keyword filter or the
    # model. The filter cannot do it -- testing for "kryt"/"nabíjačka" as a
    # substring rejects the phones that come with them -- and the model
    # answering "this is a case" costs a rate-limited request for a question
    # the title already answers.
    #
    # Damaged listings are exempt: "náhradné diely" marks BOTH a parts listing
    # and a phone sold FOR parts, and the second is something the buyer
    # actively wants at salvage pricing. Those go down the damaged path below,
    # which prices them properly, rather than being dismissed as accessories.
    if not damaged and looks_like_accessory(title):
        return {
            "decision": "reject",
            "score": 1,
            "comment": "Accessory (case, charger, cable or similar), not a device.",
            "facts": {},
        }


    facts = facts_for_listing(title, kind=kind, text=text)
    if not facts or not has_real_facts(facts):
        return None                      # unknown device -> model

    price = parse_price(price_text)
    if price is None or price <= 0:
        return None                      # no usable price -> model

    if facts.get("too_weak"):
        return {
            "decision": "reject",
            "score": 1,
            "comment": (
                f"{facts.get('chip', 'device')} is below the usable-performance "
                f"floor ({facts.get('benchmark_score', 0):,} "
                f"{facts.get('benchmark_name', 'benchmark')}); "
                f"not worth buying at any price."
            ),
            "facts": facts,
        }

    deal_at = facts.get("deal_price_eur")
    fair = facts.get("fair_price_eur")
    if not deal_at or not fair:
        return None                      # no computed price -> model

    if damaged:
        # A benchmark describes a working device, so the normal bar would
        # value a cracked phone like a good one. Broken is still worth
        # buying at salvage money, and a missed one of those costs more
        # than a wrong notification, so it is pushed on its own steeper
        # bar rather than being dropped or sent away for a second opinion.
        deal_at = round(fair * DAMAGED_PRICE_FRACTION)
        if price <= deal_at:
            return {
                "decision": "deal",
                "score": 5,
                "comment": (
                    f"{facts.get('chip', 'device')}: listed as damaged/for parts at "
                    f"EUR {price:.0f}, against EUR {fair} working ({price / fair * 100:.0f}% "
                    f"of value). Salvage price -- check what actually works."
                ),
                "facts": facts,
            }
        return None              # dearer than salvage: let the model judge

    discount = (1 - price / fair) * 100
    if price <= deal_at:
        if price < fair * TOO_GOOD_FRACTION or seller_looks_bulk(seller, price_text):
            # Real, but too cheap to be true, or one of many identically
            # priced items from the same account. Both are what a scam looks
            # like from here, and neither is something arithmetic can judge.
            return None
        return {
            "decision": "deal",
            "score": 5,
            "comment": (
                f"{facts.get('chip', 'device')}: asking EUR {price:.0f} vs computed "
                f"fair price EUR {fair} ({discount:.0f}% below); deal threshold is "
                f"EUR {deal_at}. Verified from benchmark data, not estimated."
            ),
            "facts": facts,
        }
    return {
        "decision": "reject",
        "score": 2 if discount > 25 else 1,
        "comment": (
            f"{facts.get('chip', 'device')}: asking EUR {price:.0f} vs computed fair "
            f"price EUR {fair} ({discount:.0f}% below); needs EUR {deal_at} or less. "
            f"Not a deal."
        ),
        "facts": facts,
    }


def deterministic_deal(title: str, price_text: Any, kind: str = "auto") -> Optional[Dict[str, Any]]:
    """Backwards-compatible wrapper: only the positive verdict."""
    v = deterministic_verdict(title, price_text, kind)
    return v if v and v["decision"] == "deal" else None


# A model key that is only a brand, or a brand plus a generic word, identifies
# nothing. Matching on it produced confident nonsense, so it is refused
# outright rather than fuzzily resolved.
_GENERIC_KEY_WORDS = {
    "samsung", "apple", "iphone", "ipad", "xiaomi", "huawei", "honor", "redmi",
    "realme", "oppo", "vivo", "nokia", "motorola", "lenovo", "asus", "acer",
    "dell", "hp", "msi", "lg", "sony", "google", "pixel", "oneplus", "poco",
    "galaxy", "macbook", "notebook", "laptop", "telefon", "phone", "mobil",
    "smartphone", "tablet", "pc", "computer", "pocitac", "gaming", "hidden",
    "information", "mac", "air", "pro", "mini", "plus", "ultra", "max", "lite",
}


def is_identifiable(key: str) -> bool:
    """True if a model key names a specific device rather than a brand.

    Requires either a model number ("s21", "a54", "t480") or a distinctive
    non-generic word. "samsung", "apple macbook" and "gaming pc" fail; they
    match a whole product line, not a product.
    """
    if not key:
        return False
    tokens = key.split()
    if any(any(c.isdigit() for c in t) for t in tokens):
        return True
    return any(t not in _GENERIC_KEY_WORDS for t in tokens)


def same_device(a: str, b: str) -> bool:
    """True if two model keys name the same device.

    Reuses the table matcher's identity rules: model numbers must agree
    exactly and variant words ("pro", "ultra", "fe") must be present on both
    sides or neither -- an "iPhone 12" is not an "iPhone 12 Pro Max".
    """
    if not a or not b:
        return False
    if a == b:
        return True
    try:
        from .benchmark_db import match_device
    except ImportError:  # run as a standalone file, not a package member
        try:
            from benchmark_db import match_device
        except ImportError:  # pragma: no cover
            return False
    return match_device(a, {b: 0}) is not None


def override_applies(key: str, ov_norm: str) -> bool:
    """True if a hand-written override covers this model key.

    An override may be deliberately broader than a listing key -- "galaxy s21"
    is meant to cover "Samsung Galaxy S21 5G 128GB" -- so it need not name the
    brand. It may never contradict identity though: model numbers must agree
    exactly and variant words must match, so an S21 override stays off the
    S21 Ultra and a bare "samsung" override cannot exist at all.
    """
    if not key or not ov_norm:
        return False
    if key == ov_norm:
        return True
    try:
        from .benchmark_db import (_VARIANTS, _close_to_any, _model_numbers,
                                   _signature, _tokens)
    except ImportError:  # run as a standalone file, not a package member
        try:
            from benchmark_db import (_VARIANTS, _close_to_any, _model_numbers,
                                      _signature, _tokens)
        except ImportError:  # pragma: no cover
            return False
    k_sig, o_sig = _signature(_tokens(key)), _signature(_tokens(ov_norm))
    if not o_sig or _model_numbers(k_sig) != _model_numbers(o_sig):
        return False
    if (k_sig & _VARIANTS) != (o_sig & _VARIANTS):
        return False
    # Every word the override names must be present in the key; the key may
    # carry extra words (brand, colour, capacity) the override omitted.
    return all(w in k_sig or _close_to_any(w, k_sig) for w in o_sig)


def facts_for_listing(title: str, kind: str = "auto", text: str = "") -> Dict[str, Any]:
    """Cache-only facts for a listing title, queueing a lookup on a miss.

    Called on the evaluation hot path, so it must never touch the network:
    a spec lookup takes tens of seconds and shares a rate-limited budget with
    the monitor itself. A miss records the title for `--warm-facts` and
    returns whatever is locally knowable (LineageOS support, overrides).
    """
    cache = _load(FACTS_CACHE)
    key = model_key(title)
    if not is_identifiable(key):
        # The title names no device ("Samsung"). The description still might,
        # so try the tables on the full text before giving up; only a bare
        # brand with nothing anywhere is refused. Guessing here priced a
        # "Samsung" fridge as a flagship phone and called EUR 100 a deal.
        from_text = device_facts(title, offline=True, kind=kind, text=text)
        return from_text
    hit = cache.get(key)
    if hit is None:
        # A cached entry serves this listing only if it is the SAME device.
        # Plain substring containment was not: "samsung" is contained in
        # "samsung galaxy s24 ultra", so every vague title inherited the
        # specs of whichever flagship happened to be cached.
        for cached_key, facts in cache.items():
            if cached_key and same_device(key, cached_key):
                hit = facts
                break
    if hit is not None and has_real_facts(hit):
        return hit
    try:
        pending = _load(PENDING)
        if key and key not in pending and looks_benchmarkable(title):
            pending[key] = {"title": title, "kind": kind, "seen_at": int(time.time())}
            _save(PENDING, pending)
    except OSError:
        pass
    return device_facts(title, offline=True, kind=kind, text=text)


# How many times a device may fail to resolve for a reason that is NOT the
# provider being busy before it is retired from the queue. Some titles simply
# have no answer -- a monitor, a scooter, a listing naming no model at all --
# and retrying those forever spends a shared, rate-limited budget on questions
# that will never have an answer.
MAX_WARM_ATTEMPTS = 3


def warm_cache(limit: int = 5, verbose: bool = True) -> int:
    """Resolve queued models. Run from cron/systemd, never inline.

    The batch is small on purpose: a single agentic web lookup takes tens of
    seconds, so a large batch would still be running when the next timer
    fires, and two overlapping runs would compete for the same rate limit.

    Only a BUSY provider stops the batch. An individual device that cannot be
    resolved is counted and skipped, because stopping on it blocks every entry
    behind it: the queue is walked in insertion order, so one unresolvable
    title at the head kept a 276-entry backlog at zero resolved per run for
    days while the daily quota went unused.
    """
    pending = _load(PENDING)
    if not pending:
        return 0
    done = 0
    dirty = False
    attempted = 0
    for key, info in list(pending.items()):
        if attempted >= limit:
            break
        title = info.get("title", key)
        # Entries queued under earlier classification rules can name things
        # this tool never scores (the monitor that headed the queue). Retire
        # them without spending a lookup.
        if not looks_benchmarkable(title):
            if verbose:
                print(f"  {title}: not a device we score, dropped from queue")
            pending.pop(key, None)
            dirty = True
            continue
        # Pace the batch. An agentic search costs roughly a third of the
        # per-minute token allowance, and that allowance is shared with the
        # monitor's own evaluations, so firing a batch back-to-back exhausts
        # it within seconds and the rest fails on 429 -- which is what left
        # the cache empty while the daily quota still looked healthy.
        if attempted:
            time.sleep(25)
        attempted += 1
        try:
            facts = device_facts(title, refresh=True, kind=info.get("kind", "auto"))
            if not has_real_facts(facts):
                # device_facts swallows lookup errors, so a rate-limited
                # attempt returns "successfully" with nothing in it. It now
                # records which kind it was; only the busy kind is a reason
                # to stop, and neither kind may retire a device silently on
                # a single failure.
                if facts.get("specs_busy"):
                    if verbose:
                        print(f"  {title}: provider busy, left queued")
                    break
                info["attempts"] = int(info.get("attempts", 0)) + 1
                info["last_error"] = str(facts.get("specs_error", "?"))[:120]
                dirty = True
                if info["attempts"] >= MAX_WARM_ATTEMPTS:
                    if verbose:
                        print(f"  {title}: no data after {info['attempts']} tries, "
                              f"retired ({info['last_error']})")
                    pending.pop(key, None)
                elif verbose:
                    print(f"  {title}: no data (attempt {info['attempts']}/"
                          f"{MAX_WARM_ATTEMPTS}: {info['last_error']}), left queued")
                continue
            if verbose:
                print(f"  {title}: {summarize(facts)}")
            pending.pop(key, None)
            done += 1
            dirty = True
        except RateLimited as e:
            if verbose:
                print(f"  {title}: provider busy, leaving queued ({e})")
            break  # budget is exhausted; stop rather than hammer it
        except Exception as e:
            if verbose:
                print(f"  {title}: lookup failed, dropping from queue ({e})")
            pending.pop(key, None)
            dirty = True
    if dirty:
        _save(PENDING, pending)
    # A resolved SoC serves every phone that ships it, so draining that queue
    # is worth more per lookup than resolving one more device.
    try:
        done += fill_socs(limit=3, verbose=verbose)
    except Exception as e:  # noqa: BLE001 - never let this break the run
        if verbose:
            print(f"  SoC fill skipped: {e}")
    return done


def summarize(facts: Dict[str, Any]) -> str:
    """One compact line to paste into an LLM prompt."""
    bits = [facts.get("kind", "device")]
    if facts.get("chip"):
        bits.append(str(facts["chip"]))
    if facts.get("benchmark_score"):
        bits.append(f"{facts.get('benchmark_name','benchmark')} {facts['benchmark_score']:,}")
    if facts.get("ram_gb"):
        bits.append("RAM " + "/".join(f"{r}GB" for r in facts["ram_gb"]))
    if facts.get("storage_gb"):
        bits.append("storage " + "/".join(f"{g}GB" for g in facts["storage_gb"]))
    if facts.get("used_price_eur"):
        bits.append(f"typical used ~EUR {facts['used_price_eur']}")
    if facts.get("release_year"):
        bits.append(f"released {facts['release_year']}")
    if facts.get("kind") != "laptop":
        if facts.get("lineageos_unofficial"):
            bits.append("LineageOS: unofficial build available")
        elif facts.get("lineageos_official"):
            bits.append("LineageOS: OFFICIAL support")
        else:
            bits.append("LineageOS: no official build")
    if facts.get("fair_price_eur"):
        bits.append(f"fair price ~EUR {facts['fair_price_eur']} "
                    f"(deal at <= EUR {facts['deal_price_eur']})")
    if facts.get("too_weak"):
        bits.append("TOO WEAK to be worth buying at any price")
    if facts.get("score") is not None:
        bits.append(f"QUALITY {facts['score']}/100 ({facts['verdict']})")
    elif facts.get("specs_error"):
        bits.append(f"(no specs: {str(facts['specs_error'])[:40]})")
    return "; ".join(bits)


def main() -> int:
    ap = argparse.ArgumentParser(description="Look up cached facts for a phone model.")
    ap.add_argument("model", nargs="+")
    ap.add_argument("--refresh", action="store_true", help="ignore cached value")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    ap.add_argument("--offline", action="store_true", help="cache + LineageOS only, no web lookup")
    ap.add_argument("--kind", default="auto", choices=("auto", "phone", "laptop"))
    a = ap.parse_args()
    facts = device_facts(" ".join(a.model), refresh=a.refresh, offline=a.offline, kind=a.kind)
    print(json.dumps(facts, indent=1, ensure_ascii=False) if a.json else summarize(facts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
