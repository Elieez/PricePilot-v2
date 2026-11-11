import os, json, time, random, sys, argparse, copy
from pathlib import Path
from typing import Dict, Optional
import yaml
import httpx
import scoring
from adapters import ADAPTERS

ROOT = Path(__file__).parent
STATE_DIR = ROOT / "state"
STATE_DIR.mkdir(exist_ok=True)
DUMP_FILE = ROOT / "scan-dump.json"  # written by --dry-run, read by --from-dump

DEFAULT_REFERENCE_STORES = [
    {"name": "GB", "store": "COM", "country": "GB", "lang": "en-GB", "currency": "GBP"},
    {"name": "DE", "store": "DE", "country": "DE", "lang": "de-DE", "currency": "EUR"},
]


# ---------------- Config, state, Discord ----------------

def load_cfg() -> Dict:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state(slug: str) -> Dict:
    p = STATE_DIR / f"{slug}.json"
    if p.exists():
        return json.loads(p.read_text())
    return {}


def send_discord(webhook_url: str, content: Optional[str] = None, embed: Optional[dict] = None,
                 userName: Optional[str] = None) -> bool:
    if not webhook_url:
        print("[NO WEBHOOK] would send:", content or "", (embed or {}).get("title"))
        return False
    payload = {}
    if content:
        payload["content"] = content
    if embed:
        payload["embeds"] = [embed]
    if userName:
        payload["username"] = userName
    r = httpx.post(webhook_url, json=payload, timeout=15)
    return r.status_code < 300


def excluded_brands(cfg: Dict, monitor: Dict) -> set:
    names = [*((cfg.get("filters") or {}).get("exclude_brands") or []),
             *((monitor.get("filters") or {}).get("exclude_brands") or [])]
    return {b.lower() for b in names}


# ---------------- Output ----------------

def stock_text(stock: Optional[Dict]) -> str:
    if not stock:
        return "stock unknown"
    return f"{stock['in_stock']} of {stock['sizes']} sizes in stock"


def stores_text(res: Dict, cur: str, ref_cur: Dict[str, str]) -> str:
    return ", ".join(f"{n}: {p:g} {ref_cur.get(n, '')} = ~{e:.0f} {cur}" for n, (p, e) in res["stores"].items())


def mixup_text(it: Dict, res: Dict, ref_cur: Dict[str, str]) -> str:
    """The classic error: another store's number entered as the local price."""
    same = [ref_cur.get(n) or n for n, (p, _) in res["stores"].items() if abs(p - it["price"]) < 0.5]
    return f"Same number as the {'/'.join(same)} price: looks like a currency mix-up" if same else ""


def print_hit(n: int, it: Dict, res: Dict, stock: Optional[Dict], ref_cur: Dict[str, str]):
    cur = it.get("currency") or ""
    print(f"\n#{n}  {it.get('title')}")
    print(f"    {it['price']:.0f} {cur}, expected about {res['expected']:.0f} {cur} "
          f"({(1 - res['ratio']) * 100:.0f}% below, saves {res['saving']:.0f} {cur})")
    print(f"    {it.get('brand')}  -  {stores_text(res, cur, ref_cur)}")
    print(f"    {stock_text(stock)}")
    if mixup_text(it, res, ref_cur):
        print(f"    {mixup_text(it, res, ref_cur)}")
    print(f"    {it['url']}")


def scan_embed(it: Dict, res: Dict, stock: Optional[Dict], ref_cur: Dict[str, str], monitor_name: str,
               sold_out: bool = False) -> Dict:
    cur = it.get("currency") or ""
    label = "⚫ SOLD OUT price error" if sold_out else "🚨 Possible price error"
    mixup = mixup_text(it, res, ref_cur)
    embed = {
        "title": f"{label}: {it.get('title') or 'item'}"[:256],
        "url": it["url"],
        "color": 0x757575 if sold_out else 0xE53935,
        "description": (
            f"**{it['price']:.0f} {cur}** vs ~{res['expected']:.0f} {cur} expected "
            f"(−{(1 - res['ratio']) * 100:.0f}%, saves {res['saving']:.0f} {cur})\n"
            f"{it.get('brand') or 'Unknown brand'} • {stock_text(stock)}\n"
            f"Other stores: {stores_text(res, cur, ref_cur)}"
            + ("\n" + mixup if mixup else "")
        ),
        "footer": {"text": monitor_name},
    }
    if it.get("image_url"):
        embed["image"] = {"url": it["image_url"]}
    return embed


# ---------------- Scan runner ----------------

def run_scan_monitor(cfg: Dict, monitor: Dict, webhook: str, dry_run: bool = False,
                     from_dump: bool = False, include_sold_out: bool = False) -> Optional[bool]:
    """Returns True if state was written, False if not, None if the scan failed.
    A failed or partial scan never sends alerts and never touches state."""
    dry_run = dry_run or from_dump  # saved data may be stale: never alert from it
    slug = monitor["slug"]
    sc = {**scoring.DEFAULTS, **(monitor.get("scoring") or {})}
    ref_stores = monitor.get("reference_stores") or DEFAULT_REFERENCE_STORES
    ref_cur = {r["name"]: r.get("currency", "") for r in ref_stores}

    # One shared pause for all stores: every request after the first waits.
    lo, hi = (cfg.get("run") or {}).get("jitter_ms", [800, 1600])
    count = [0]
    def pause():
        if count[0]:
            time.sleep(random.uniform(lo, hi) / 1000.0)
        count[0] += 1
        if count[0] % 10 == 0:
            print(f"  ... {count[0]} requests", flush=True)

    make = ADAPTERS[monitor["adapter"]]
    adapter = make(monitor, pause=pause)
    ref_adapters = {r["name"]: make({**monitor, **r}, pause=pause) for r in ref_stores}

    state = load_state(slug)
    alerted: Dict = copy.deepcopy(state.get("alerted") or {})  # id -> [price alerted at, last seen day]
    # Reference prices found by id for items a reference store does not list in
    # this category: id -> [SE price when looked up, {store: price or None}, last seen day].
    # Looked up again only when the SE price changes, which is when an error can appear.
    cache: Dict = copy.deepcopy(state.get("refs") or {})
    today = int(time.time() // 86400)

    try:
        if from_dump:
            # Re-score the last dry run's data: instant, and no requests to ASOS.
            dump = json.loads(DUMP_FILE.read_text(encoding="utf-8")).get(slug) if DUMP_FILE.exists() else None
            if not dump or "refs" not in dump:
                print(f"[{slug}] no saved two-store scan; run --dry-run once first.")
                return None
            items = {int(k): v for k, v in dump["items"].items()}
            refs = {n: {int(k): v for k, v in d.items()} for n, d in dump["refs"].items()}
            stock = {int(k): v for k, v in dump.get("stock", {}).items()}
        else:
            items = adapter.scan()
            if not items:
                raise ValueError("scan returned no products")
            refs = {}
            budget = int(sc.get("max_lookup_requests", 80))
            for name, ra in ref_adapters.items():
                listed = {pid: it["price"] for pid, it in ra.scan().items() if it.get("price")}
                if not listed:
                    raise ValueError(f"{name} scan returned no products")
                need = []
                for pid, it in items.items():
                    if pid in listed:
                        continue
                    e = cache.get(str(pid))
                    if not e or e[0] != it["price"]:
                        e = cache[str(pid)] = [it["price"], {}, today]
                    if name not in e[1]:
                        need.append(pid)
                need = need[:max(budget, 0) * ra.LOOKUP_BATCH]  # the rest is picked up next run
                budget -= -(-len(need) // ra.LOOKUP_BATCH)
                found = ra.stock_prices(need) if need else {}
                for pid in need:
                    cache[str(pid)][1][name] = (found.get(pid) or {}).get("price")
                refs[name] = {pid: listed.get(pid) or (cache.get(str(pid)) or [0, {}])[1].get(name)
                              for pid in items}
                refs[name] = {pid: p for pid, p in refs[name].items() if p}

        ratios = scoring.store_ratios(items, refs, sc["min_matched"])

        skip = excluded_brands(cfg, monitor)
        hits = []
        for pid, it in items.items():
            res = scoring.score_item(it, {n: refs[n].get(pid) for n in refs}, ratios, sc)
            if not res or (it.get("brand") or "").lower() in skip:
                continue
            last = alerted.get(str(pid))
            if last is not None and it["price"] > last[0] * 0.9:
                continue  # already reported at (roughly) this price
            hits.append((it, res))
        hits.sort(key=lambda h: -h[1]["saving"])

        # The search API has no stock data, so ask for it, but only for the hits.
        if not from_dump:
            stock = adapter.stock_prices([it["id"] for it, _ in hits[:adapter.LOOKUP_BATCH]]) if hits else {}
    except Exception as e:
        print(f"[{slug}] scan failed after {count[0]} requests: {e}")
        return None

    if dry_run and not from_dump:
        dumps = json.loads(DUMP_FILE.read_text(encoding="utf-8")) if DUMP_FILE.exists() else {}
        dumps[slug] = {"items": items, "refs": refs, "stock": stock}
        DUMP_FILE.write_text(json.dumps(dumps, ensure_ascii=False), encoding="utf-8")

    live, gone, sold = [], [], []
    for it, res in hits:
        s = stock.get(it["id"])
        sold_out = s is not None and s["in_stock"] == 0
        corrected = s is not None and s.get("price") and s["price"] > it["price"] * 1.01
        (gone if sold_out or corrected else live).append((it, res))
        if sold_out and not corrected:
            sold.append((it, res))

    no_ref = sum(1 for pid in items if not any(refs[n].get(pid) for n in refs))
    print(f"[{slug}] {len(items)} items, {no_ref} with no reference price, {count[0]} requests; "
          + ", ".join(f"1 {ref_cur[n] or n} = {r:.2f} {monitor.get('currency', 'SEK')}" for n, r in ratios.items())
          + f"; {len(live)} hits, {len(gone)} sold out or already corrected")

    cap = int(sc.get("max_alerts_per_run", 10))
    if dry_run:
        print(f"\n{len(live)} item(s) would be alerted.")
        for n, (it, res) in enumerate(live, 1):
            print_hit(n, it, res, stock.get(it["id"]), ref_cur)
        if gone:
            print(f"\n{len(gone)} more not alerted (sold out or price already corrected):")
            for n, (it, res) in enumerate(gone, 1):
                print_hit(n, it, res, stock.get(it["id"]), ref_cur)
        return False

    # Optional: also report sold-out errors (a manual run uses this to prove the
    # whole chain works). They are not marked as alerted, so a restock still alerts.
    if include_sold_out:
        for it, res in sold[:cap]:
            try:
                send_discord(webhook, userName="PricePilot",
                             embed=scan_embed(it, res, stock.get(it["id"]), ref_cur, monitor["name"], sold_out=True))
            except Exception as e:
                print(f"[{slug}] discord error: {e}")
            time.sleep(0.5)

    for it, res in live[:cap]:
        try:
            ok = send_discord(webhook, content=monitor.get("mention_on_bug") or None, userName="PricePilot",
                              embed=scan_embed(it, res, stock.get(it["id"]), ref_cur, monitor["name"]))
        except Exception as e:
            print(f"[{slug}] discord error: {e}")
            ok = False
        # Undelivered hits (cap, webhook failure) and sold-out ones are not marked,
        # so they are tried again next run.
        if ok:
            alerted[str(it["id"])] = [it["price"], today]
        time.sleep(0.5)

    # Forget items that left the category a while ago, and lookups no longer needed.
    keep_days = int(sc.get("keep_days", 14))
    for store in (alerted, cache):
        for k in list(store):
            if int(k) in items:
                store[k][-1] = today
            elif today - store[k][-1] > keep_days:
                del store[k]

    new_state = {"alerted": alerted, "refs": cache}
    if new_state == {"alerted": state.get("alerted") or {}, "refs": state.get("refs") or {}}:
        return False
    (STATE_DIR / f"{slug}.json").write_text(
        json.dumps(new_state, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="scan and print what would be alerted; send nothing, change no state")
    ap.add_argument("--from-dump", action="store_true",
                    help="like --dry-run, but re-score the data saved by the last dry run (no requests)")
    ap.add_argument("--include-sold-out", action="store_true",
                    help="also send sold-out price errors, marked as such (they stay eligible for a restock alert)")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")  # Windows consoles choke on some product names

    cfg = load_cfg()
    webhook = os.getenv("DISCORD_WEBHOOK") or ""
    if not webhook and not (args.dry_run or args.from_dump):
        print("DISCORD_WEBHOOK is not set; refusing to run (use --dry-run to test).")
        sys.exit(1)

    failed = False
    for m in cfg["monitors"]:
        print(f"→ Running: {m['name']}")
        result = run_scan_monitor(cfg, m, webhook, dry_run=args.dry_run, from_dump=args.from_dump,
                                  include_sold_out=args.include_sold_out)
        failed = failed or result is None
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
