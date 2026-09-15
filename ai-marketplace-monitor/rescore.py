"""Re-settle already-notified listings with the current local rules.

Every stored verdict was reached under the rules in force at the time, and
most were reached by a language model guessing a resale value. The benchmark
tables and price curves have since learned to answer a lot of those locally:
iPhones and iPads got a table, chips got a curve of their own, and the
matching stopped confusing brands.

This walks the notified listings and re-runs `deterministic_verdict` on each.
Where arithmetic can now settle a listing, its stored rating is replaced with
the computed one; where it cannot, the existing verdict is left untouched.
No network, no AI, no quota -- it is all cache and tables, so it is safe to
run as often as you like.

    ai-marketplace-monitor --rescore            # show what would change
    ai-marketplace-monitor --rescore --apply    # write it
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Tuple

from diskcache import Cache  # type: ignore

from .ai import AIResponse
from .device_facts import deterministic_verdict
from .listing import Listing
from .utils import CacheType, amm_home
from .webui.found_export import _normalize_notified

SCRIPT_VERDICT_NAME = "benchmark tables"


def _notified(cache: Cache) -> List[Tuple[tuple, str, str, Any]]:
    """(key, marketplace, listing_id, (date, hash, price)) for every alert."""
    rows = []
    for key in list(cache.iterkeys()):
        if (isinstance(key, tuple) and len(key) >= 4
                and key[0] == CacheType.USER_NOTIFIED.value):
            rows.append((key, key[1], key[2], _normalize_notified(cache.get(key))))
    return rows


def rescore(apply: bool = False, verbose: bool = True,
            local_cache: Cache | None = None) -> Dict[str, int]:
    """Re-rate notified listings from local data. Returns a summary."""
    cache = local_cache if local_cache is not None else Cache(str(amm_home))
    stats = {"rescored": 0, "unchanged": 0, "needs_model": 0, "missing": 0}
    changes: List[str] = []

    for _key, marketplace, listing_id, (_date, listing_hash, price) in _notified(cache):
        if not listing_hash:
            stats["missing"] += 1
            continue
        url = f"https://www.facebook.com/marketplace/item/{listing_id}/"
        listing = Listing.from_cache(url, cache)
        if listing is None:
            stats["missing"] += 1
            continue

        verdict = deterministic_verdict(
            listing.title,
            price if price is not None else listing.price,
            text=getattr(listing, "description", "") or "",
            seller=getattr(listing, "seller", "") or "",
        )
        if verdict is None:
            # Condition, damage and scam signals are the model's job; a
            # listing the arithmetic cannot settle keeps whatever it has.
            stats["needs_model"] += 1
            continue

        existing = None
        for k in list(cache.iterkeys()):
            if (isinstance(k, tuple) and len(k) >= 4
                    and k[0] == CacheType.AI_INQUIRY.value and k[3] == listing_hash):
                value = cache.get(k)
                if isinstance(value, dict):
                    existing = value
                    break
        if (existing and existing.get("name") == SCRIPT_VERDICT_NAME
                and existing.get("comment") == verdict["comment"]):
            stats["unchanged"] += 1
            continue

        stats["rescored"] += 1
        changes.append(
            f"  [{existing.get('score') if existing else '-'} "
            f"{(existing or {}).get('name', '-')}] -> [{verdict['score']} script]  "
            f"{listing.title[:44]}\n      {verdict['comment'][:96]}"
        )
        if apply:
            # Every OTHER verdict for this listing has to go, or the row join
            # may pick the stale one back up: it keys on the listing hash and
            # takes whichever entry it meets first.
            for k in list(cache.iterkeys()):
                if (isinstance(k, tuple) and len(k) >= 4
                        and k[0] == CacheType.AI_INQUIRY.value
                        and k[3] == listing_hash):
                    cache.delete(k)
            res = AIResponse(score=verdict["score"], comment=verdict["comment"],
                             name=SCRIPT_VERDICT_NAME)
            cache.set(
                (CacheType.AI_INQUIRY.value, "rescore", "rescore", listing_hash),
                asdict(res), tag=CacheType.AI_INQUIRY.value,
            )

    if verbose:
        for line in changes:
            print(line)
        print(f"\n{'APPLIED' if apply else 'DRY RUN'}: "
              f"{stats['rescored']} rescored, {stats['unchanged']} already current, "
              f"{stats['needs_model']} still need the model, "
              f"{stats['missing']} not in cache")
    return stats
