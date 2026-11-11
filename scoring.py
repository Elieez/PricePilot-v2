"""Price-anomaly scoring.

Instead of "is this item new and is its brand allowed?", every item is compared
with what that kind of item normally costs:

  1. same brand + same product type  (e.g. adidas Originals sneakers)   -> high confidence
  2. product type, scaled by how expensive the brand usually is         -> medium
  3. product type only                                                  -> low

A 119 SEK ASOS DESIGN t-shirt is normal for ASOS DESIGN t-shirts and scores ~1.0.
A 110 SEK Samba against a ~1400 SEK sneaker baseline scores 0.08 and is alerted.
No brand list has to be maintained for that to work.
"""
from collections import defaultdict
from statistics import median
from typing import Dict, Iterable, Optional

DEFAULTS = {
    "deal_ratio": 0.45,       # alert when price <= 45% of the reference price
    "bug_ratio": 0.30,        # ... and call it a likely price error at <= 30%
    "min_saving": 250,        # reference - price, in the store currency
    "drop_pct": 40,           # alert when a tracked item drops this much between runs
    "min_group": 4,           # items needed before a brand+type / type baseline is trusted
    "min_brand": 3,           # items needed before a brand's price level is trusted
}


def _brand(it: Dict) -> str:
    return (it.get("brand") or "").strip().lower()


def regular_price(it: Dict) -> float:
    """Price before any markdown ASOS itself declares, so sales don't drag baselines down."""
    return max(it.get("price") or 0, it.get("previous") or 0, it.get("rrp") or 0)


def build_baselines(items: Iterable[Dict], min_group: int = 4, min_brand: int = 3) -> Dict:
    by_type, by_brand_type = defaultdict(list), defaultdict(list)
    typed = [it for it in items if it.get("type_id") and it.get("price")]
    for it in typed:
        reg = regular_price(it)
        by_type[it["type_id"]].append(reg)
        by_brand_type[(_brand(it), it["type_id"])].append(reg)

    type_med = {t: median(v) for t, v in by_type.items() if len(v) >= min_group}
    brand_type = {k: (median(v), len(v)) for k, v in by_brand_type.items() if len(v) >= min_group}

    # How expensive is this brand relative to everything else of the same type?
    rel = defaultdict(list)
    for it in typed:
        if it["type_id"] in type_med:
            rel[_brand(it)].append(regular_price(it) / type_med[it["type_id"]])
    brand_factor = {b: (median(v), len(v)) for b, v in rel.items() if len(v) >= min_brand}

    return {"type": type_med, "brand_type": brand_type, "brand_factor": brand_factor}


def expected_price(it: Dict, bl: Dict) -> Optional[Dict]:
    t, b = it.get("type_id"), _brand(it)
    if not t:
        return None
    if (b, t) in bl["brand_type"]:
        med, n = bl["brand_type"][(b, t)]
        return {"ref": med, "confidence": "high", "basis": f"median of {n} similar items from this brand"}
    if t in bl["type"]:
        if b in bl["brand_factor"]:
            factor, n = bl["brand_factor"][b]
            return {"ref": bl["type"][t] * factor, "confidence": "medium",
                    "basis": f"type median scaled by brand price level ({n} items)"}
        return {"ref": bl["type"][t], "confidence": "low", "basis": "type median (brand too rare to judge)"}
    return None


def score_item(it: Dict, bl: Dict, last_price: Optional[float], cfg: Optional[Dict] = None) -> Optional[Dict]:
    """Return an alert dict if the item is priced far below its reference, else None."""
    c = {**DEFAULTS, **(cfg or {})}
    price = it.get("price")
    if not price or price <= 0:
        return None

    declared = max(it.get("previous") or 0, it.get("rrp") or 0)
    if last_price and price <= last_price * (1 - c["drop_pct"] / 100):
        # Most specific signal there is: this exact item was more expensive last run.
        ref = {"ref": last_price, "confidence": "high", "basis": "its own price on the previous check", "kind": "drop"}
    elif declared > price:
        ref = {"ref": declared, "confidence": "high", "basis": "ASOS list price", "kind": "sale"}
    else:
        ref = expected_price(it, bl)
        if ref is None:
            return None
        ref["kind"] = "baseline"

    ratio = price / ref["ref"]
    saving = ref["ref"] - price
    # With only a type baseline, a rare brand that is simply cheap looks like a
    # bargain (379 SEK Pull&Bear sneakers vs a 1400 SEK type median), so it has to
    # be extreme to count: half the price-error ratio.
    if ref["kind"] == "drop":
        limit = 1.0  # drop_pct was the test, and it already passed
    else:
        limit = c["bug_ratio"] / 2 if ref["confidence"] == "low" else c["deal_ratio"]
    if ratio > limit or saving < c["min_saving"]:
        return None

    if ref["kind"] == "sale":
        tier = "sale"
    elif ratio <= c["bug_ratio"]:
        tier = "bug"
    else:
        tier = "drop" if ref["kind"] == "drop" else "deal"
    return {**ref, "ratio": ratio, "saving": saving, "tier": tier}
