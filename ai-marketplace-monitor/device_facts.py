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
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

CACHE_DIR = Path.home() / ".ai-marketplace-monitor"
FACTS_CACHE = CACHE_DIR / "device_facts.json"
# Hand-maintained truth that beats anything looked up. For facts you know and
# the automated sources get wrong or cannot see (unofficial ROM support,
# benchmark scores the model misremembers). Same shape as a cache entry.
OVERRIDES = CACHE_DIR / "device_facts_overrides.json"
LINEAGE_CACHE = CACHE_DIR / "lineageos_devices.json"
LINEAGE_TTL = 7 * 24 * 3600  # the supported-device list changes slowly
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Agentic models with server-side web search. compound-mini is tried first:
# full compound pulls in far more search context and gets rejected outright
# (HTTP 413) whenever the per-minute token budget is partly used, which on a
# free tier is most of the time.
GROQ_SEARCH_MODELS = ("groq/compound-mini", "groq/compound")
UA = "ai-marketplace-monitor device-facts"


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


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


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
SPEC_PROMPT = (
    "Look up this phone model and report ONLY verified facts you find on the web: "
    "{model}\n\n"
    "Reply as strict JSON with exactly these keys and no prose:\n"
    '{{"model": "canonical name", "ram_gb": [list of RAM variants in GB, integers], '
    '"antutu": integer or null, "antutu_version": "v9|v10|unknown", '
    '"release_year": integer or null, "used_price_eur": integer or null, '
    '"source": "short note on where the numbers came from"}}\n'
    "used_price_eur = typical second-hand price in Europe in EUR for a unit in "
    "good working condition. Use null for anything you could not verify."
)


def fetch_specs(model: str, api_key: str, timeout: int = 45) -> Dict[str, Any]:
    """Ask a web-searching model for this device's specs, as JSON.

    Groq answers 413 (not 429) when a request would exceed the remaining
    tokens-per-minute allowance, so both codes mean "wait", not "give up".
    """
    last_err: Optional[Exception] = None
    for model_name in GROQ_SEARCH_MODELS:
        for attempt in range(1):
            payload = {
                "model": model_name,
                "messages": [{"role": "user", "content": SPEC_PROMPT.format(model=model)}],
            }
            req = urllib.request.Request(
                GROQ_URL,
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": UA,
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    body = json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (413, 429):
                    break
                break
            except urllib.error.URLError as e:
                last_err = e
                break

            content = body["choices"][0]["message"].get("content") or ""
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if not m:
                last_err = ValueError(f"no JSON in response: {content[:150]}")
                break
            facts = json.loads(m.group(0))
            facts["looked_up_by"] = model_name
            return facts
    raise last_err or RuntimeError("spec lookup failed")


def device_facts(model: str, refresh: bool = False, offline: bool = False) -> Dict[str, Any]:
    """Facts for one model. Cached, so each model costs one lookup ever.

    offline=True skips the (slow, rate-limited) web spec lookup and returns
    whatever is cached plus LineageOS support, which is a fast local check.
    Use that on any hot path; warm the cache separately.
    """
    cache = _load(FACTS_CACHE)
    key = _norm(model)
    if not refresh and key in cache:
        return cache[key]

    facts: Dict[str, Any] = {"query": model}
    api_key = "" if offline else os.environ.get("GROQ_API_KEY", "")
    if api_key:
        try:
            facts.update(fetch_specs(model, api_key))
        except Exception as e:  # network/quota/parse - degrade, never crash
            facts["specs_error"] = str(e)[:200]
    else:
        facts["specs_error"] = "offline" if offline else "GROQ_API_KEY not set"

    lo = lineage_supported(model)
    facts["lineageos_official"] = bool(lo)
    if lo:
        facts["lineageos_device"] = f"{lo['vendor']} {lo['name']} ({lo['codename']})".strip()

    # Manual overrides win over everything above.
    for ov_key, ov in _load(OVERRIDES).items():
        if ov_key and ov_key in key:
            facts.update(ov)
            facts["overridden"] = True

    facts["cached_at"] = int(time.time())
    cache[key] = facts
    _save(FACTS_CACHE, cache)
    return facts


def summarize(facts: Dict[str, Any]) -> str:
    """One compact line to paste into an LLM prompt."""
    bits = []
    if facts.get("ram_gb"):
        bits.append("RAM " + "/".join(f"{r}GB" for r in facts["ram_gb"]))
    if facts.get("antutu"):
        bits.append(f"AnTuTu ~{facts['antutu']:,} ({facts.get('antutu_version','?')})")
    if facts.get("used_price_eur"):
        bits.append(f"typical used price ~EUR {facts['used_price_eur']}")
    if facts.get("release_year"):
        bits.append(f"released {facts['release_year']}")
    if facts.get("lineageos_unofficial"):
        bits.append("LineageOS: unofficial build available")
    elif facts.get("lineageos_official"):
        bits.append("LineageOS: OFFICIAL support")
    else:
        bits.append("LineageOS: no official build (unofficial may still exist)")
    if facts.get("specs_error"):
        bits.append(f"(spec lookup failed: {facts['specs_error'][:60]})")
    return "; ".join(bits)


def main() -> int:
    ap = argparse.ArgumentParser(description="Look up cached facts for a phone model.")
    ap.add_argument("model", nargs="+")
    ap.add_argument("--refresh", action="store_true", help="ignore cached value")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    ap.add_argument("--offline", action="store_true", help="cache + LineageOS only, no web lookup")
    a = ap.parse_args()
    facts = device_facts(" ".join(a.model), refresh=a.refresh, offline=a.offline)
    print(json.dumps(facts, indent=1, ensure_ascii=False) if a.json else summarize(facts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
