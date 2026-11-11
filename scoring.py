"""Cross-store price-error scoring.

ASOS sells the same product (same id) in every store. Across the ~4,000 items of
a category the SEK price is a very steady multiple of the GBP and EUR prices
(about 14.3 and 11.5 in Nov 2026; 98% of items within +-25%), so that multiple
is measured from the run itself as a median and no FX API is needed.

A price error breaks the multiple: the Samba that was 110 GBP in the UK and
110 SEK in Sweden sat at 0.07 of the expected ratio, while the lowest normal
item in the category was at 0.70.

An item is only flagged when EVERY reference store that sells it agrees, so a
wrong price in one reference store cannot cause an alert on its own. Items that
no reference store sells are not judged at all: comparing with sibling products
could not tell a price error from a cheaper kind of product.
"""
from statistics import median
from typing import Dict, Optional

DEFAULTS = {
    "error_ratio": 0.5,    # alert when price <= 50% of what every reference store implies
    "min_saving": 250,     # expected - price, in the store currency
    "min_matched": 50,     # items needed in common before a store's ratio is trusted
}


def store_ratios(items: Dict[int, Dict], refs: Dict[str, Dict[int, float]], min_matched: int = 50) -> Dict[str, float]:
    """Median local-price / reference-price per reference store.
    Raises ValueError when a store has too few items in common to be trusted."""
    out = {}
    for name, prices in refs.items():
        rs = [it["price"] / prices[pid] for pid, it in items.items()
              if it.get("price") and prices.get(pid)]
        if len(rs) < min_matched:
            raise ValueError(f"only {len(rs)} items in common with {name} (need {min_matched})")
        out[name] = median(rs)
    return out


def score_item(it: Dict, ref_prices: Dict[str, Optional[float]], ratios: Dict[str, float],
               cfg: Optional[Dict] = None) -> Optional[Dict]:
    """Return an alert dict if the item is far below what all reference stores imply."""
    c = {**DEFAULTS, **(cfg or {})}
    price = it.get("price")
    if not price or price <= 0:
        return None
    # store -> (its price there, what that implies here)
    stores = {n: (p, p * ratios[n]) for n, p in ref_prices.items() if p and n in ratios}
    if not stores:
        return None
    expected = min(e for _, e in stores.values())  # the most cautious store decides
    ratio = price / expected
    if ratio > c["error_ratio"] or expected - price < c["min_saving"]:
        return None
    return {"ratio": ratio, "expected": expected, "saving": expected - price, "stores": stores}
