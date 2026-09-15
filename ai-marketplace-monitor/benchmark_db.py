#!/usr/bin/env python3
"""Bulk benchmark database, downloaded once and looked up offline.

PassMark publishes complete, current lists as plain HTML tables:

    cpu_list.php      ~2,800 desktop/laptop CPUs   (CPU Mark)
    gpu_list.php      ~2,800 GPUs                  (G3D Mark)
    device_list.php   ~5,200 Android phones        (PassMark device score)

That is the whole population -- CPUs and phone models are finite and
already published, so there is no reason to ask a language model about
them one at a time. One 300KB download answers every lookup instantly,
offline, for free, and deterministically.

Refresh with:  ai-marketplace-monitor --update-benchmarks
"""
from __future__ import annotations

import difflib
import html
import http.cookiejar
import json
import re
import time
import unicodedata
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    from .utils import amm_home as CACHE_DIR
except ImportError:  # running standalone
    CACHE_DIR = Path.home() / ".ai-marketplace-monitor"

DB_PATH = CACHE_DIR / "benchmarks.json"
# Entries learned from the web for devices PassMark does not list. Kept in a
# separate file so a re-download of the upstream tables never discards them,
# and so it can be inspected or hand-edited like any other data file.
LOCAL_PATH = CACHE_DIR / "benchmarks_local.json"

# Plausible ranges, used to reject a bad answer before it is written. A wrong
# number that is stored is worse than a missing one: it is then trusted
# offline, forever, without another look.
VALID_RANGE = {
    "device": (500, 60_000),        # PassMark Android; table max is ~31,000
    "device_cpu": (100, 40_000),    # Android CPU Mark; table max is ~19,000
    "device_ios": (100, 60_000),    # iOS CPU Mark; table max is ~27,000
    "cpu": (100, 200_000),          # PassMark CPU Mark; table max is ~131,000
    "gpu": (50, 100_000),           # G3D Mark; table max is ~42,000
    "antutu": (20_000, 4_000_000),  # AnTuTu v10 totals
}
MAX_AGE = 30 * 24 * 3600  # PassMark updates continuously; monthly is plenty
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"

CPU_PAGE_URL = "https://www.cpubenchmark.net/CPU_mega_page.html"
CPU_DATA_URL = "https://www.cpubenchmark.net/data/"

SOURCES = {
    # cpu_list.php serves ONE vendor per page and defaults to Intel: it gave
    # 2,844 entries, every one of them Intel, so every Ryzen machine silently
    # had no benchmark at all. The mega page's own data feed carries the whole
    # list (6,700+ parts, AMD included) as JSON.
    "cpu": (CPU_DATA_URL, None),
    "gpu": ("https://www.videocardbenchmark.net/gpu_list.php",
            r'<TR id="gpu\d+"><TD><A HREF="[^"]*">([^<]+)</A></TD><TD>([\d,]+)</TD>'),
    "device": ("https://www.androidbenchmark.net/device_list.php",
               r'<tr><td><a href="/phone\.php\?phone=[^"]*">([^<]+)</a></td>'
               r'<td><a href="passmark_lookup\.php[^"]*">([\d,]+)</a></td>'),
    # A second phone table, listing CPU Mark instead of the overall device
    # score. Worth having because its coverage differs -- it carries models
    # the device list omits (the Galaxy S20 among them) -- and it is the
    # closest thing to a per-SoC number that is actually published in bulk.
    # Different markup: <li> blocks, not table rows.
    "device_cpu": ("https://www.androidbenchmark.net/cpumark_chart.html", None),
    # iPhones and iPads. The Android lists are Android-only, so 40 iPhone, 26
    # iPad and 38 MacBook listings -- 42% of everything queued -- had no
    # offline answer at all and every one of them fell through to the
    # rate-limited web lookup, which is why the queue never drained. Same
    # publisher and the same <li> chart markup, so it costs one more fetch.
    # (MacBooks are already covered: Apple silicon is in the CPU table.)
    "device_ios": ("https://www.iphonebenchmark.net/cpumark_chart.html", None),
}


def _fetch(url: str, timeout: int = 60, referer: str = "") -> str:
    headers = {"User-Agent": UA}
    if referer:
        # The JSON feed answers with an empty list unless the request looks
        # like the table on the page asking for its own rows.
        headers.update({
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _fetch_cpu_json(timeout: int = 120) -> Dict[str, int]:
    """The full CPU list, both vendors, from the mega page's data feed.

    Needs a session: fetch the page first so the feed sees a cookie, then ask
    for the rows the way the page does. Without that it returns {"data": []}
    with a 200, which would look like a successful download of nothing.
    """
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    page_req = urllib.request.Request(CPU_PAGE_URL, headers={"User-Agent": UA})
    with opener.open(page_req, timeout=timeout):
        pass                      # discard the HTML; we only want the cookie
    data_req = urllib.request.Request(CPU_DATA_URL, headers={
        "User-Agent": UA,
        "Referer": CPU_PAGE_URL,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })
    with opener.open(data_req, timeout=timeout) as r:
        blob = json.loads(r.read().decode("utf-8", "replace"))
    rows: Dict[str, int] = {}
    for row in blob.get("data", []):
        name, mark = html.unescape(str(row.get("name", "")).strip()), row.get("cpumark")
        try:
            score = int(str(mark).replace(",", ""))
        except (TypeError, ValueError):
            continue              # "NA" for parts with no submissions
        if name and score > 0:
            rows[name] = score
    return rows


def _parse_chart(page: str) -> Dict[str, int]:
    """Parse the <li>-based chart pages (name and score in separate spans)."""
    rows: Dict[str, int] = {}
    for block in page.split('<li id="rk')[1:]:
        # Tolerate stray whitespace/attributes in the tag: the iOS chart emits
        # `<span class="prdname" >` and the Android one does not, so a regex
        # anchored on `">` silently parsed zero rows from an otherwise perfect
        # 96KB page.
        name = re.search(r'<span class="prdname"[^>]*>([^<]+)</span>', block)
        count = re.search(r'<span class="count"[^>]*>([\d,]+)</span>', block)
        if name and count:
            rows[html.unescape(name.group(1)).strip()] = int(count.group(1).replace(",", ""))
    return rows


def download(verbose: bool = True) -> Dict[str, Dict[str, int]]:
    """Fetch and parse every list. A few MB, once."""
    tables: Dict[str, Dict[str, int]] = {}
    for kind, (url, pattern) in SOURCES.items():
        try:
            if kind == "cpu":
                tables[kind] = _fetch_cpu_json()
                if verbose:
                    print(f"  {kind}: {len(tables[kind])} entries")
                continue
            page = _fetch(url)
        except Exception as e:  # noqa: BLE001 - keep whatever else succeeded
            if verbose:
                print(f"  {kind}: download failed ({e})")
            tables[kind] = {}
            continue
        rows = (
            _parse_chart(page) if pattern is None
            else {
                html.unescape(n).strip(): int(v.replace(",", ""))
                for n, v in re.findall(pattern, page, re.I)
            }
        )
        tables[kind] = rows
        if verbose:
            print(f"  {kind}: {len(rows)} entries")
    return tables


def save(tables: Dict[str, Dict[str, int]]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched_at": time.time(), "tables": tables}))
    tmp.replace(DB_PATH)


def load_local() -> Dict[str, Dict[str, int]]:
    try:
        return json.loads(LOCAL_PATH.read_text()).get("tables", {})
    except (OSError, ValueError):
        return {}


def add_entry(kind: str, name: str, score: int, source: str = "web") -> bool:
    """Record a looked-up benchmark so it is never looked up again.

    Returns False (and stores nothing) if the value is implausible for its
    kind -- a stored wrong number would be trusted offline from then on.
    """
    lo, hi = VALID_RANGE.get(kind, (0, 10**9))
    if not isinstance(score, int) or isinstance(score, bool) or not lo <= score <= hi:
        return False
    name = (name or "").strip()
    if not name:
        return False
    blob: Dict[str, Any]
    try:
        blob = json.loads(LOCAL_PATH.read_text())
    except (OSError, ValueError):
        blob = {"tables": {}, "meta": {}}
    blob.setdefault("tables", {}).setdefault(kind, {})[name] = score
    blob.setdefault("meta", {})[f"{kind}:{name}"] = {"source": source, "at": int(time.time())}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LOCAL_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(blob, indent=1, ensure_ascii=False))
    tmp.replace(LOCAL_PATH)
    return True


def load(auto_download: bool = True, verbose: bool = False) -> Dict[str, Dict[str, int]]:
    def _merge(tables: Dict[str, Dict[str, int]]) -> Dict[str, Dict[str, int]]:
        # Locally learned entries win: they exist precisely because upstream
        # had nothing.
        merged = {k: dict(v) for k, v in tables.items()}
        for kind, rows in load_local().items():
            merged.setdefault(kind, {}).update(rows)
        return merged

    try:
        blob = json.loads(DB_PATH.read_text())
        if blob.get("fetched_at", 0) + MAX_AGE > time.time():
            return _merge(blob.get("tables", {}))
    except (OSError, ValueError):
        blob = None
    if not auto_download:
        return (blob or {}).get("tables", {})
    tables = download(verbose=verbose)
    if any(tables.values()):
        save(tables)
        return _merge(tables)
    return _merge((blob or {}).get("tables", {}))


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------
def _norm(s: str) -> str:
    """Lowercase, fold accents, reduce to alphanumerics.

    Folding matters: stripping non-ASCII turns "Predám" into "pred m", two
    tokens that match nothing and drag every similarity score down.
    """
    folded = unicodedata.normalize("NFKD", s.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


# Capacity ("128GB") is not part of a model name; a model number ("S21",
# "Note 12") is, and confusing the two is how "Redmi Note 12 Pro" matched
# the entirely different "Redmi Pro".
_CAPACITY = re.compile(r"^\d+(gb|tb|mb)$")
# Model number glued to its variant word: "13pro" -> "13 pro", "3gen" -> "3 gen".
# THE definition -- device_facts tokenizes through _tokens rather than keeping a
# second copy, because when those two drifted a device silently lost its specs.
_GLUED_VARIANT_RE = re.compile(
    r"(?<=\d)(?=(?:pro|max|mini|lite|ultra|plus|note|edge|fold|flip|air|neo|prime|xl"
    r"|gen|generation)\b)",
    re.IGNORECASE,
)
_BRANDS = {
    "samsung", "apple", "iphone", "ipad", "macbook", "xiaomi", "redmi", "poco",
    "huawei", "honor", "realme", "oppo", "vivo", "oneplus", "nokia", "motorola",
    "lenovo", "thinkpad", "asus", "acer", "dell", "hp", "msi", "lg", "sony",
    "google", "pixel", "nothing", "zte", "alcatel", "tcl", "meizu", "infinix",
    "tecno", "umidigi", "doogee", "blackview", "cubot", "ulefone",
}


_VENDOR_FILLER = {
    "intel", "amd", "apple", "core", "processor", "cpu", "gpu", "with",
    "graphics", "mobile", "series", "nvidia", "geforce", "radeon",
}


# Sales words and colours that appear in listings but never in a model name.
_LISTING_NOISE = {
    # "nova" is NOT here: Slovak "nová" means new, but it is also Huawei's
    # product line, and dropping it let "Huawei nova 10 pro" be priced as a
    # "Huawei Mate 10 Pro" -- a 2022 mid-ranger valued as a 2017 flagship.
    # A slightly noisier key costs less than pricing the wrong phone.
    "predam", "predavam", "novy", "nove", "uplne", "super", "stav",
    "zanovny", "top", "lacno", "mobil", "telefon", "smartfon", "cierny",
    "biely", "modry", "zeleny", "cierna", "biela", "black", "white", "blue",
    "green", "grey", "gray", "silver", "gold", "red", "pink", "purple",
    "gb", "tb", "ram", "ssd", "dual", "sim", "5g", "4g", "lte",
}


def _tokens(s: str) -> list:
    # Sellers write the configuration as "8/256", "8 GB / 256 GB" or
    # "6GB/128GB". Those digits are RAM and storage, not model numbers, and
    # reading them as identity made "Samsung A54 8/256" fail to match the
    # plain "Samsung Galaxy A54". Drop the pair outright.
    s = re.sub(r"\b\d{1,4}\s*(?:gb|tb)?\s*/\s*\d{1,4}\s*(?:gb|tb)?\b", " ", s, flags=re.I)
    # "128 GB" is likewise one fact; join it so it reads as capacity.
    s = re.sub(r"(\d+)\s*(gb|tb|mb)\b", r"\1\2", s, flags=re.I)
    # A variant word glued to the model number ("13pro", "12mini", "200lite")
    # is one token here but two in every table entry, so the phrase index --
    # which matches exact token sequences -- could never hit it. Split it for
    # the same reason device_facts.model_key does; both sides must prepare
    # text identically or a phrase present in both fails to match.
    s = _GLUED_VARIANT_RE.sub(" ", s)
    return _norm(s).split()


def _query_model_tokens(tokens) -> list:
    return [t for t in tokens if t not in _LISTING_NOISE and not _CAPACITY.match(t)]


def _entry_tokens(name: str) -> list:
    """Tokens of a table entry, minus the bits a listing never repeats.

    PassMark qualifies entries in ways sellers do not: a clock suffix
    ("@ 1.70GHz") and a variant in brackets ("(Exynos)"). Requiring those
    would reject the correct row for almost every real listing.
    """
    name = re.sub(r"\(.*?\)", " ", name)
    name = re.sub(r"@.*$", " ", name)
    return _tokens(name)


def _model_numbers(tokens) -> set:
    """Tokens carrying a digit that are not a storage size."""
    return {t for t in tokens if any(c.isdigit() for c in t) and not _CAPACITY.match(t)}


# Words a seller may or may not type. Their absence must not sink a match
# ("Samsung S21" is the same phone as "Samsung Galaxy S21"), and their
# presence must not carry one either.
_FILLER = {
    "galaxy", "phone", "mobile", "smartphone", "5g", "4g", "lte", "dual",
    "sim", "various", "models", "edition", "version",
    # Connectivity and form descriptors, not product lines. PassMark writes
    # "(WiFi)" / "(Cellular)" on iPads and sellers write "Wi-Fi" anywhere;
    # treating those as identifying made "apple ipad 6 wi fi" a different
    # device from "apple ipad 6".
    "wi", "fi", "wifi", "cellular", "gsm", "esim", "nfc",
}


# Words that name a DIFFERENT product in the same line. If one side has one
# and the other does not, they are not the same device -- an S21 Ultra is
# not an S21, and a Note is not a plain model.
_VARIANTS = {
    "ultra", "plus", "pro", "max", "mini", "lite", "fe", "neo", "prime",
    "note", "edge", "fold", "flip", "air", "se", "xl",
}


def _signature(tokens) -> set:
    return {t for t in tokens if t not in _FILLER and not _CAPACITY.match(t)}


# Words that belong to a model name despite carrying no digit. A token that is
# neither one of these nor digit-bearing cannot be part of the model.
_MODEL_WORDS = {
    "pro", "max", "mini", "lite", "ultra", "plus", "fe", "neo", "prime", "note",
    "edge", "fold", "flip", "air", "se", "xl", "5g", "4g", "lte", "wifi",
}


def trim_to_model(tokens) -> list:
    """Drop the prose that trails a model name.

    A listing ends in whatever the seller wanted to say -- "NA OPRAVU", "v
    záruke", "Top stav". Those words are unexplained by any table entry, and
    the similarity score pays for every one of them: "Iphone 12 Pro Max NA
    OPRAVU" scored 0.571 against "Apple iPhone 12 Pro Max" and missed the 0.6
    bar, so a phone that IS in the table resolved to nothing.

    A model name ends at the last token that either carries a digit or is a
    variant word; the rest is description, in whatever language.
    """
    last = -1
    for i, tok in enumerate(tokens):
        if any(c.isdigit() for c in tok) or tok in _MODEL_WORDS:
            last = i
    return list(tokens[:last + 1]) if last >= 0 else list(tokens)


def _entry_identity(sig) -> set:
    """The words in a signature that actually name a product.

    A model number and a variant word are shared by dozens of unrelated
    devices; what distinguishes them is a brand or product-line word. An entry
    with none of those cannot identify anything on its own.
    """
    return {w for w in sig
            if w not in _VARIANTS and not any(c.isdigit() for c in w)}


# --- Phrase index -------------------------------------------------------
#
# Fuzzy-matching an extracted model name against 16k entries is both slow and
# guessy: it has a similarity threshold, and any threshold admits nonsense.
# "iPhone 15 Pro" scored 0.67 against a junk Android entry literally named
# "Pro 15" -- same model number, same variant word, one word unexplained --
# and came back as a 60k-AnTuTu device.
#
# Inverting it removes the guessing. Index every known product name by its
# token phrase once, then look for those phrases INSIDE the listing text.
# A hit is an exact phrase, not a similarity score, so word order matters:
# "15 pro" is not "pro 15". It is also a plain dict lookup per n-gram, so
# scanning a whole description costs microseconds.
#
# This runs first; the fuzzy matcher stays as the fallback for listings whose
# wording no index entry covers.

_PHRASE_INDEX: Dict[str, Tuple[Dict[str, Tuple[str, int]], int]] = {}

# Phrases too generic to identify a product on their own. Without this, an
# entry named "Android 001" or a bare brand would match half the marketplace.
_MIN_PHRASE_TOKENS = 2


# Words either side may freely omit: line names, radios, vendor boilerplate
# and the brand itself. A seller writes "Galaxy S21", PassMark writes "Samsung
# Galaxy S21 5G", and neither is more correct than the other.
_OPTIONAL = _FILLER | _VENDOR_FILLER | _BRANDS | _LISTING_NOISE

# Cap on how many optional words one phrase may drop. Every subset is indexed,
# so this bounds the work at 2**5; longer names simply keep their tail.
_MAX_OPTIONAL_DROPPED = 5


def prepare(text: str) -> list:
    """The one text preparation both sides go through.

    Index entries and listing text MUST be prepared identically, or a phrase
    present in both fails to match on a difference neither side chose. That
    happened: listing text had "5g" stripped as noise while index entries kept
    it, so "galaxy s21 5g" and "galaxy s21" existed on opposite sides and the
    phone went unrecognised.

    Lowercases, folds Slovak accents to ASCII, turns every non-alphanumeric
    character into a space, collapses runs of spaces, and joins capacities
    ("128 GB" -> "128gb") so they read as one fact.
    """
    return _tokens(text)


def _phrase_forms(tokens) -> list:
    """Every reading of a name once optional words are dropped.

    Enumerating the subsets, rather than a few fixed combinations, is what
    makes the two sides agree: whichever optional words the seller left out,
    some indexed form matches exactly. Word order is never touched, which is
    what still keeps "15 pro" from matching an entry named "Pro 15".
    """
    core = [t for t in tokens if not _CAPACITY.match(t)]
    optional = [i for i, t in enumerate(core) if t in _OPTIONAL]
    forms, seen = [], set()
    for mask in range(1 << min(len(optional), _MAX_OPTIONAL_DROPPED)):
        dropped = {optional[i] for i in range(min(len(optional), _MAX_OPTIONAL_DROPPED))
                   if mask >> i & 1}
        form = [t for i, t in enumerate(core) if i not in dropped]
        phrase = " ".join(form)
        if form and phrase not in seen:
            seen.add(phrase)
            forms.append(form)
    return forms


def _index_phrases(name: str) -> set:
    """The phrases a listing might plausibly spell this entry as.

    PassMark writes "Samsung Galaxy S21 5G (Exynos)" and "Intel Core i5-8350U
    @ 1.70GHz"; a seller writes neither. Index the qualified form, the plain
    one, and every reading of both with optional words dropped.
    """
    phrases = set()
    for tokens in (prepare(name), _entry_tokens(name)):
        for form in _phrase_forms(tokens):
            phrases.add(" ".join(form))
    # A phrase must carry a model number and two words, or it names a product
    # line rather than a product and would match half the marketplace.
    #
    # It must ALSO keep a word that identifies the product. Brands count as
    # optional above, so "Google Pixel 10 Pro" was indexed all the way down to
    # "10 pro" -- two words, carries a digit -- and a "Huawei nova 10 pro"
    # listing hit it and was priced as a Pixel, clearing the deal bar at EUR
    # 129 and notifying. A number and a variant word are shared by dozens of
    # unrelated phones; the brand or product line is the part that says WHICH.
    keep = set()
    for p in phrases:
        toks = p.split()
        if len(toks) < _MIN_PHRASE_TOKENS or not any(c.isdigit() for c in p):
            continue
        # The phrase must still say WHICH product. Brands count as optional
        # above, so "Google Pixel 10 Pro" was also indexed as "10 pro" -- two
        # words, carries a digit -- and a "Huawei nova 10 pro" listing hit it
        # and was priced as a Pixel, clearing the deal bar and notifying.
        #
        # Two token shapes identify something: a word ("pixel", "galaxy") and
        # a part number, which mixes letters and digits ("i5", "8350u",
        # "s21"). A bare number and a variant word do not -- dozens of
        # unrelated products share "10" and "pro".
        if _entry_identity(set(toks)) or any(
            any(c.isalpha() for c in t) and any(c.isdigit() for c in t) for t in toks
        ):
            keep.add(p)
    return keep


def phrase_index(kind: str, table: Dict[str, int]) -> Tuple[Dict[str, Tuple[str, int]], int]:
    """Build (phrase -> (name, score), longest phrase) once per table."""
    cached = _PHRASE_INDEX.get(kind)
    if cached and cached[2] == len(table):
        return cached[0], cached[1]
    index: Dict[str, Tuple[str, int]] = {}
    longest = 0
    for name, score in table.items():
        for phrase in _index_phrases(name):
            prev = index.get(phrase)
            # Two entries can share a phrase ("... S21 5G" exists for both the
            # Exynos and Snapdragon build). Keep the lower score: overstating
            # performance is what turns a mediocre phone into a false "deal".
            if prev is None or score < prev[1]:
                index[phrase] = (name, score)
            longest = max(longest, len(phrase.split()))
    _PHRASE_INDEX[kind] = (index, longest, len(table))
    return index, longest


def find_in_text(text: str, kind: str, table: Dict[str, int]) -> Optional[Tuple[str, int]]:
    """Longest known product name occurring in `text`, or None.

    Longest wins so "Galaxy S21 Ultra" beats "Galaxy S21" on a listing that
    says Ultra -- the shorter phrase is a prefix of the real product and
    would silently value a different phone.
    """
    if not text or not table:
        return None
    index, longest = phrase_index(kind, table)
    raw = prepare(text)
    listing_variants = set(raw) & _VARIANTS
    listing_brands = set(raw) & _BRANDS
    best = None
    for toks in _phrase_forms(raw):
        for n in range(min(longest, len(toks)), _MIN_PHRASE_TOKENS - 1, -1):
            if best and n <= best[0]:
                break            # a longer phrase already won
            for i in range(len(toks) - n + 1):
                hit = index.get(" ".join(toks[i:i + n]))
                # The phrase may have been indexed with the brand dropped, so
                # check the brand of the ENTRY it resolves to: a listing that
                # says Huawei must not match a Google entry, however well the
                # rest of the phrase lines up.
                if hit and listing_brands:
                    entry_brands = set(_tokens(hit[0])) & _BRANDS
                    if entry_brands and not (entry_brands & listing_brands):
                        continue
                # A variant word names a different, dearer product. "Galaxy
                # S21 Ultra" contains the phrase "Galaxy S21", so without
                # this an Ultra would be valued as the base model.
                if hit and (set(_tokens(hit[0])) & _VARIANTS) == listing_variants:
                    best = (n, hit)
                    break
            if best and best[0] == n:
                break
    return best[1] if best else None


def match_device(query: str, table: Dict[str, int]) -> Optional[Tuple[str, int]]:
    """Fuzzy-match a product name, tolerating how sellers actually write them.

    Strict subset matching was too brittle: it needed every word of the
    table entry to appear in the listing, so "Samsung S21 128GB" missed
    "Samsung Galaxy S21" over the dropped word "Galaxy".

    Instead:
      * model numbers must agree EXACTLY -- "s21" is not "s22", and an
        "s21 ultra" is not an "s21". This is the discriminating signal and
        is never fuzzy-matched.
      * remaining words are compared as sets, with filler ignored, and
        scored by how much each side leaves unexplained.
      * close spellings still count, via difflib, so "thinkpad"/"think pad"
        or a stray plural does not break a match.
    """
    q = _tokens(query)
    q_sig = _signature(_query_model_tokens(q))
    q_nums = _model_numbers(q_sig)
    if not q_sig:
        return None

    q_brands = q_sig & _BRANDS
    best, best_score = None, 0.0
    for name, score in table.items():
        e_sig = _signature(_entry_tokens(name))
        if not e_sig:
            continue
        # An entry that names nothing but a variant word and a number ("Pro
        # 15", "s24", "i15proMax") identifies no product. Such an entry leaves
        # only the BRAND unexplained when compared with a real listing --
        # "iPhone 15 Pro" against "Pro 15" scores 0.67 and clears the bar --
        # and the brand is the most discriminating word there is. That match
        # priced an iPhone 15 Pro as a 60,500-AnTuTu device and called a
        # flagship "not worth buying at any price". 22 of ~5,200 entries are
        # this shape; none of them can identify a device on their own.
        if not _entry_identity(e_sig):
            continue
        # Brands disagree -> different product, however well the rest scores.
        e_brands = e_sig & _BRANDS
        if q_brands and e_brands and not (q_brands & e_brands):
            continue
        # Product LINES disagree -> also a different product. "Huawei nova 10
        # Pro" against "Huawei Mate 10 Pro" agrees on brand, number and
        # variant, leaving only "nova" vs "mate" unexplained -- which scores
        # exactly 0.6 and passed. Those are a 2022 mid-ranger and a 2017
        # flagship. When both sides name a line and the names are unrelated,
        # no similarity score should rescue it.
        # The product LINE has to be explained by the entry. "Honor Magic 7
        # Lite" against "Honor 7 Lite" agrees on brand, number and variant and
        # leaves only "magic" unexplained -- which scores exactly 0.6 and used
        # to pass, valuing a 2024 phone as a 2016 one. Requiring the entry to
        # account for every line word catches that, and also "Huawei nova 10
        # Pro" against "Huawei Mate 10 Pro", where both sides name a line and
        # the names are unrelated.
        q_line = _entry_identity(q_sig) - _BRANDS
        e_line = _entry_identity(e_sig) - _BRANDS
        unexplained = {w for w in q_line
                       if w not in e_line and not _close_to_any(w, e_line)}
        if unexplained:
            continue
        # Model numbers are the identity of the device: require exact agreement.
        if _model_numbers(e_sig) != q_nums:
            continue
        # A variant word on one side only means a different product.
        if (e_sig & _VARIANTS) != (q_sig & _VARIANTS):
            continue
        missing = e_sig - q_sig      # entry claims words the listing lacks
        extra = q_sig - e_sig        # listing has words the entry lacks
        # Allow a near-spelling to count as present before penalising it.
        missing = {m for m in missing
                   if not _close_to_any(m, q_sig)}
        extra = {x for x in extra if not _close_to_any(x, e_sig)}
        denom = max(len(e_sig | q_sig), 1)
        sim = 1.0 - (len(missing) + len(extra)) / denom
        if sim > best_score:
            best, best_score = (name, score), sim
    return best if best_score >= 0.6 else None


def _close_to_any(word: str, others, threshold: float = 0.86) -> bool:
    """True if `word` is a near-spelling of any token in `others`."""
    return any(difflib.SequenceMatcher(None, word, o).ratio() >= threshold
               for o in others)


def match_component(query: str, table: Dict[str, int]) -> Optional[Tuple[str, int]]:
    """Match a part named inside a longer listing, e.g. a CPU in a laptop ad.

    The listing carries model numbers the part name does not (the laptop's
    own), so only one direction can be required: the part's distinctive
    words must appear in the listing.
    """
    qset = set(_tokens(query))
    best, best_len = None, 0
    for name, score in table.items():
        et = [t for t in _entry_tokens(name) if t not in _VENDOR_FILLER]
        if not et or len(et) <= best_len:
            continue
        if set(et) <= qset:
            best, best_len = (name, score), len(et)
    return best


def _lookup(text: str, kind: str, fuzzy) -> Optional[Tuple[str, int]]:
    """Exact phrase first, fuzzy match second."""
    table = load(auto_download=False).get(kind, {})
    return find_in_text(text, kind, table) or fuzzy(text, table)


def lookup_device(title: str) -> Optional[Tuple[str, int]]:
    """PassMark overall score for an Android phone, by listing text."""
    return _lookup(title, "device", match_device)


def lookup_device_cpu(title: str) -> Optional[Tuple[str, int]]:
    """Android CPU Mark, for phones the overall-score table omits."""
    return _lookup(title, "device_cpu", match_device)


def lookup_device_ios(title: str) -> Optional[Tuple[str, int]]:
    """CPU Mark for an iPhone or iPad, which no Android table lists.

    Strict matching ONLY -- deliberately no find_in_text pass. Apple sells one
    product name across many generations, and the table spells the generation
    out ("iPad Pro 11.0 3rd Gen"). A phrase search finds the generation-less
    "Apple iPad Pro 12.9" inside a 5th-generation listing and returns 2,120
    where the real entry is 23,475: an order of magnitude low, which reads as
    "too weak to buy at any price". match_device requires the model numbers to
    agree exactly, so a generation the listing names must be named by the
    entry too. Fewer hits, no wrong ones.
    """
    table = load(auto_download=False).get("device_ios", {})
    # Strict matching charges for every word the entry cannot explain, so
    # trailing seller prose alone can sink a phone that IS listed. Try the
    # title as written, then the model name with that prose trimmed off.
    return (match_device(title, table)
            or match_device(" ".join(trim_to_model(_tokens(title))), table))


def lookup_cpu(text: str) -> Optional[Tuple[str, int]]:
    return _lookup(text, "cpu", match_component)


def lookup_gpu(text: str) -> Optional[Tuple[str, int]]:
    return _lookup(text, "gpu", match_component)


def main() -> int:
    print("Downloading benchmark tables from PassMark...")
    tables = download()
    if not any(tables.values()):
        print("Nothing downloaded; keeping any existing database.")
        return 1
    save(tables)
    total = sum(len(t) for t in tables.values())
    print(f"Saved {total} entries to {DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
