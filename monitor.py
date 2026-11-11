import os, json, time, random, sys, argparse
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional
import yaml
import httpx
import scoring
from adapters import ADAPTERS

ROOT = Path(__file__).parent
STATE_DIR = ROOT / "state"
STATE_DIR.mkdir(exist_ok=True)

# ---------------- FX (currency) ----------------


def _load_fx_cache() -> Optional[Dict]:
    p = STATE_DIR / "fx.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    return None


def _save_fx_cache(data: Dict):
    (STATE_DIR / "fx.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))


def fetch_fx_rates(cfg_fx: Dict) -> Optional[Dict]:
    """Fetch EUR-based rates and cache; return {'base':'EUR','rates':{...}, 'fetched_at': iso}."""
    provider = (cfg_fx or {}).get("provider", "exchangerate_host")
    symbols = ",".join((cfg_fx or {}).get("symbols", ["SEK","EUR","USD","GBP"]))
    try:
        if provider == "exchangerate_host":
            url = "https://api.exchangerate.host/latest"
            r = httpx.get(url, params={"base": "EUR", "symbols": symbols}, timeout=20)
            r.raise_for_status()
            data = r.json()
            out = {"base": "EUR", "rates": data.get("rates", {}), "fetched_at": time.time()}
            _save_fx_cache(out)
            return out
    except Exception as e:
        print("[FX] fetch failed:", e)
        return None


def get_fx(cfg: Dict) -> Optional[Dict]:
    """Return fx dict, refresh if older than refresh_hours."""
    fx_cfg = cfg.get("fx") or {}
    cache = _load_fx_cache()
    refresh_hours = int(fx_cfg.get("refresh_hours", 24))
    now = time.time()
    if cache and (now - cache.get("fetched_at", 0) < refresh_hours * 3600):
        return cache
    return fetch_fx_rates(fx_cfg)


def to_sek_cents(amount_cents: int, currency: Optional[str], fx: Optional[Dict]) -> Optional[int]:
    if amount_cents is None:
        return None
    if not currency or currency.upper() == "SEK":
        return int(amount_cents)
    if not fx or not fx.get("rates"):
        return None
    rates = fx["rates"]
    cur = currency.upper()
    if cur not in rates or "SEK" not in rates:
        return None
    # EUR-based: amount_SEK = amount * (SEK_per_EUR / CUR_per_EUR)
    sek_per_eur = float(rates["SEK"])
    cur_per_eur = float(rates[cur])
    factor = sek_per_eur / cur_per_eur
    return int(round(amount_cents * factor))


# ---------------- Config & state ----------------

def load_cfg() -> Dict:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state(slug: str) -> Dict:
    p = STATE_DIR / f"{slug}.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"seen": []}


def save_state(slug: str, state: Dict):
    p = STATE_DIR / f"{slug}.json"
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def send_discord(webhook_url: str,
                 content: str | None = None,
                 embed: dict | None = None,
                 userName: str | None = None,
                 avatar: str | None = None):
    if not webhook_url:
        print("[DRY ALERT]", content or embed)
        return True
    payload = {}
    if content: 
        payload["content"] = content
    if embed: 
        payload["embeds"] = [embed]
    if userName:
        payload["username"] = userName
    if avatar:
        payload["avatar_url"] = avatar
    r = httpx.post(webhook_url, json=payload, timeout=15)
    return r.status_code < 300


# ---------------- Filtering ----------------

def merge_filters(global_f: Dict, monitor_f: Dict) -> Dict:
    gf = global_f or {}
    mf = monitor_f or {}
    out = {
        "include_brands": [*(gf.get("include_brands") or []), *(mf.get("include_brands") or [])],
        "exclude_brands": [*(gf.get("exclude_brands") or []), *(mf.get("exclude_brands") or [])],
        "require_discount_pct": mf.get("require_discount_pct", gf.get("require_discount_pct", 0)),
    }
    # normalize lowercase and de-dup
    out["include_brands"] = list(dict.fromkeys([b.lower() for b in out["include_brands"]]))
    out["exclude_brands"] = list(dict.fromkeys([b.lower() for b in out["exclude_brands"]]))
    return out


def brand_ok(brand: Optional[str], flt: Dict) -> bool:
    b = (brand or "").lower()
    if b in (flt.get("exclude_brands") or []):
        return False
    inc = flt.get("include_brands") or []
    if inc and b not in inc:
        return False
    return True


def discount_ok(offer: Dict, flt: Dict) -> bool:
    req = int(flt.get("require_discount_pct", 0) or 0)
    if req <= 0:
        return True

    cur = offer.get("price_cents")
    prev = offer.get("prev_price_cents")

    if cur is None:
        # No current price parsed → block (safer)
        return False

    if prev is None:
        # Fail-open when previous price is unknown (keep True).
        # If you want to require a visible discount, change this to: return False
        return True

    if prev <= cur:
        # No discount or price increased → block
        return False

    drop = (prev - cur) / prev * 100
    return drop >= req


# ---------------- Runner ----------------

def run_monitor(cfg: Dict, monitor: Dict, webhook: str, fx: Optional[Dict]):
    slug = monitor["slug"]
    Adapter = ADAPTERS[monitor["adapter"]]
    adapter = Adapter(monitor)

    state = load_state(slug)
    seen = set(state.get("seen", []))

    sample_limit = (cfg.get("run") or {}).get("sample_limit_per_run", 30)
    jitter_lo, jitter_hi = ((cfg.get("run") or {}).get("jitter_ms", [800, 1600]))

    flt = merge_filters(cfg.get("filters"), monitor.get("filters"))
    want_sek = (cfg.get("currency_output") or "SEK").upper() == "SEK"

    new_urls: List[str] = []
    for listing in monitor["listing_urls"]:
        try:
            urls = adapter.discover_urls(listing, sample_limit)
        except Exception as e:
            print(f"[{slug}] listing error: {e}")
            continue
        for url in urls:
            if url in seen:
                continue
            new_urls.append(url)

    if not new_urls:
        print(f"[{slug}] No new listings.")
        return False

    changed = False
    for url in new_urls:
        try:
            offer = adapter.fetch_offer(url) or {"title": url, "price_cents": None, "currency": ""}
            if not brand_ok(offer.get("brand"), flt):
                seen.add(url)
                changed = True
                continue
            if not discount_ok(offer, flt):
                seen.add(url)
                changed = True
                continue
            # Currency conversion (default to SEK output)
            price_cents = offer.get("price_cents")
            cur_code = (offer.get("currency") or "").upper()
            sek_cents = to_sek_cents(price_cents, cur_code, fx) if want_sek else None

            orig_str = f"{price_cents/100:.2f} {cur_code}" if isinstance(price_cents, int) else "(price n/a)"

            if want_sek and isinstance(sek_cents, int):
                price_str = f"{sek_cents/100:.0f} SEK"
                if isinstance(price_cents, int):
                    price_str += f" (orig {orig_str})"
            else: 
                price_str = orig_str

            brand_str = f" | {offer.get('brand')}" if offer.get("brand") else ""
            
            drop_pct = None
            prev = offer.get("prev_price_cents")
            curp = offer.get("price_cents")
            if isinstance(prev, int) and isinstance(curp, int) and prev > 0 and prev > curp:
                drop_pct = int(round((prev - curp) * 100 / prev))

            img_url = offer.get("image_url")

            embed = {
                "title": offer.get("title") or "New item",
                "url": url,
                "description": (
                    f"{offer.get('brand') or ''} • {price_str}"
                    + (f" • ↓{drop_pct}%" if drop_pct is not None else "")
                ).strip(),
                "footer": {"text": monitor["name"]},
            }
            if offer.get("image_url"):
                embed["image"] = {"url": offer["image_url"]}

            send_discord(webhook, embed=embed, userName="PricePilot")

            seen.add(url)
            changed = True
        except Exception as e:
            print(f"[{slug}] product error: {e}")
        time.sleep(random.uniform(jitter_lo, jitter_hi) / 1000.0)

    if changed:
        save_state(slug, {"seen": sorted(seen)})
    return changed

# ---------------- Scan runner (whole category, scored) ----------------

TIER_STYLE = {
    "bug": ("🚨 Possible price error", 0xE53935),
    "deal": ("💰 Far below typical price", 0xFB8C00),
    "drop": ("📉 Sudden price drop", 0xFB8C00),
    "sale": ("🔥 Deep sale", 0x1E88E5),
}


def scan_embed(it: Dict, res: Dict, type_name: Optional[str], monitor_name: str) -> Dict:
    label, color = TIER_STYLE[res["tier"]]
    cur = it.get("currency") or ""
    embed = {
        "title": f"{label}: {it.get('title') or 'item'}"[:256],
        "url": it["url"],
        "color": color,
        "description": (
            f"**{it['price']:.0f} {cur}** vs ~{res['ref']:.0f} {cur} "
            f"(−{(1 - res['ratio']) * 100:.0f}%, saves {res['saving']:.0f} {cur})\n"
            f"{it.get('brand') or 'Unknown brand'} • {type_name or 'unknown type'}\n"
            f"Compared with: {res['basis']}"
        ),
        "footer": {"text": monitor_name},
    }
    if it.get("image_url"):
        embed["image"] = {"url": it["image_url"]}
    return embed


def run_scan_monitor(cfg: Dict, monitor: Dict, webhook: str, dry_run: bool = False) -> bool:
    slug = monitor["slug"]
    lo, hi = (cfg.get("run") or {}).get("jitter_ms", [800, 1600])
    adapter = ADAPTERS[monitor["adapter"]](monitor, pause=lambda: time.sleep(random.uniform(lo, hi) / 1000.0))
    sc = {**scoring.DEFAULTS, **(monitor.get("scoring") or {})}
    flt = merge_filters(cfg.get("filters"), monitor.get("filters"))

    state = load_state(slug)
    known: Dict = state.get("items") or {}      # id -> [price, type_id, last_seen_day]
    alerted: Dict = state.get("alerted") or {}  # id -> price we alerted at

    # Any failure here aborts the run without touching state, so a blocked or
    # half-finished scan can never be mistaken for "everything changed".
    try:
        items, facet_values = adapter.scan()
        if not items:
            print(f"[{slug}] scan returned no products; skipping.")
            return False
        for pid, it in items.items():
            old = known.get(str(pid))
            it["type_id"] = old[1] if old else None
        unknown = {pid for pid, it in items.items() if not it["type_id"]}
        if unknown:
            per_type = Counter(it["type_id"] for it in items.values() if it["type_id"])
            for pid, tid in adapter.resolve_types(unknown, facet_values, per_type).items():
                items[pid]["type_id"] = tid
    except Exception as e:
        print(f"[{slug}] scan error after {adapter.requests} requests: {e}")
        return False

    type_names = {fv["id"]: fv.get("name") for fv in facet_values}
    baselines = scoring.build_baselines(items.values(), sc["min_group"], sc["min_brand"])

    hits = []
    for pid, it in items.items():
        old = known.get(str(pid))
        res = scoring.score_item(it, baselines, old[0] if old else None, sc)
        if not res or not brand_ok(it.get("brand"), flt):
            continue
        last_alert = alerted.get(str(pid))
        if last_alert is not None and it["price"] > last_alert * 0.9:
            continue  # already reported at (roughly) this price
        hits.append((it, res))
    hits.sort(key=lambda h: -h[1]["saving"])

    untyped = sum(1 for it in items.values() if not it["type_id"])
    print(f"[{slug}] {len(items)} items, {len(unknown)} new, {untyped} without type, "
          f"{adapter.requests} requests, {len(hits)} hits")

    if dry_run:
        for it, res in hits[: int(sc.get("max_alerts_per_run", 10)) * 3]:
            print(f"  {res['tier']:<4} {it['price']:>6.0f} vs {res['ref']:>6.0f} "
                  f"({res['ratio']:.2f}, {res['confidence']}) {it.get('brand')} | "
                  f"{type_names.get(it['type_id'])} | {it.get('title')} | {it['url']}")
        return False

    sent = set()
    for it, res in hits[: int(sc.get("max_alerts_per_run", 10))]:
        mention = monitor.get("mention_on_bug") if res["tier"] == "bug" else None
        try:
            ok = send_discord(webhook, content=mention, userName="PricePilot",
                              embed=scan_embed(it, res, type_names.get(it["type_id"]), monitor["name"]))
        except Exception as e:
            print(f"[{slug}] discord error: {e}")
            ok = False
        if ok:
            sent.add(it["id"])
            alerted[str(it["id"])] = it["price"]
        time.sleep(0.5)

    today = int(time.time() // 86400)
    new_items = {str(pid): [it["price"], it["type_id"] or None, today] for pid, it in items.items()}
    # Hits that were not delivered (per-run cap, webhook failure) keep their old
    # price so the drop is detected again on the next run.
    for it, _ in hits:
        if it["id"] not in sent and str(it["id"]) in known:
            new_items[str(it["id"])][0] = known[str(it["id"])][0]
    # Items that left the category are remembered for a while in case they return.
    keep_days = int(sc.get("keep_days", 14))
    for k, v in known.items():
        if k not in new_items and today - v[2] <= keep_days:
            new_items[k] = v

    new_state = {"items": new_items, "alerted": {k: v for k, v in alerted.items() if k in new_items}}
    if new_state == {"items": known, "alerted": state.get("alerted") or {}}:
        return False
    (STATE_DIR / f"{slug}.json").write_text(
        json.dumps(new_state, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="scan and print what would be alerted; send nothing, save nothing")
    args = ap.parse_args()

    cfg = load_cfg()
    webhook = os.getenv("DISCORD_WEBHOOK") or cfg.get("discord_webhook")

    fx = None
    if (cfg.get("currency_output") or "SEK").upper() == "SEK":
        fx = get_fx(cfg)

    any_changed = False
    for m in cfg["monitors"]:
        print(f"→ Running: {m['name']}")
        if hasattr(ADAPTERS[m["adapter"]], "scan"):
            changed = run_scan_monitor(cfg, m, webhook, dry_run=args.dry_run)
        elif args.dry_run:
            print("  (dry run is only supported for scan monitors; skipped)")
            changed = False
        else:
            changed = run_monitor(cfg, m, webhook, fx)
        if changed:
            any_changed = True

    sys.exit(0 if any_changed else 78)

if __name__ == "__main__":
    main()