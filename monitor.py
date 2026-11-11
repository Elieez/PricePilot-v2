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

UNDELIVERED = []  # ids of alerts Discord did not accept; makes the run exit non-zero

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
    # Never print the exception or the URL: the webhook URL is a secret.
    try:
        r = httpx.post(webhook_url, json=payload, timeout=15)
    except Exception as e:
        print(f"[discord] send failed: {type(e).__name__}")
        return False
    if r.status_code >= 300:
        print(f"[discord] send failed: HTTP {r.status_code} {r.text[:200]}")
        return False
    print(f"[discord] sent: {(embed or {}).get('title', '')}")
    return True


def check_webhook(webhook_url: str) -> Optional[str]:
    """Return None if Discord recognises the webhook, else what is wrong.
    A GET on a webhook URL only describes it; nothing is posted."""
    url = webhook_url.strip()
    if url != webhook_url:
        return "the value has spaces or line breaks around it"
    if not url.startswith("https://") or "/api/webhooks/" not in url:
        return "the value is not a Discord webhook URL (https://discord.com/api/webhooks/...)"
    try:
        r = httpx.get(url, timeout=15)
    except Exception as e:
        return f"could not reach Discord ({type(e).__name__})"
    if r.status_code != 200:
        return f"Discord answered HTTP {r.status_code}: the webhook was deleted or the URL is incomplete"
    return None


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


EMBED_STYLE = {
    "new": ("🚨 Possible price error", 0xE53935),
    "sold_out": ("⚫ SOLD OUT price error", 0x757575),
}


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
               kind: str = "new") -> Dict:
    cur = it.get("currency") or ""
    label, color = EMBED_STYLE[kind]
    mixup = mixup_text(it, res, ref_cur)
    embed = {
        "title": f"{label}: {it.get('title') or 'item'}"[:256],
        "url": it["url"],
        "color": color,
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
                     from_dump: bool = False) -> Optional[bool]:
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
    # Products already reported; each is reported once and never again.
    # id -> [price alerted at, "in" or "out" (stock at that moment), last seen day]
    alerted: Dict = {k: (v if len(v) == 3 else [v[0], "in", v[1]])
                     for k, v in copy.deepcopy(state.get("alerted") or {}).items()}
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
            # Products to check even when no scanned category lists them (one request each).
            for pid in monitor.get("watch_ids") or []:
                if pid not in items:
                    found = adapter.find(int(pid))
                    if found and found.get("price"):
                        items[int(pid)] = found
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
        hits, known = [], 0
        for pid, it in items.items():
            res = scoring.score_item(it, {n: refs[n].get(pid) for n in refs}, ratios, sc)
            if not res or (it.get("brand") or "").lower() in skip:
                continue
            if str(pid) in alerted:
                known += 1  # one alert per product, ever
                continue
            hits.append((it, res))
        hits.sort(key=lambda h: -h[1]["saving"])

        # The search API has no stock data, so ask for it, but only for new hits.
        if not from_dump:
            stock = adapter.stock_prices([it["id"] for it, _ in hits[:adapter.LOOKUP_BATCH]]) if hits else {}
    except Exception as e:
        print(f"[{slug}] scan failed after {count[0]} requests: {e}")
        return None

    if dry_run and not from_dump:
        dumps = json.loads(DUMP_FILE.read_text(encoding="utf-8")) if DUMP_FILE.exists() else {}
        dumps[slug] = {"items": items, "refs": refs, "stock": stock}
        DUMP_FILE.write_text(json.dumps(dumps, ensure_ascii=False), encoding="utf-8")

    # A new error is sent once, marked sold out if it is. With alert_sold_out off,
    # a sold-out error waits unreported until it can be bought.
    notify_sold_out = bool(sc.get("alert_sold_out", True))
    plan, quiet = [], 0  # (item, result, kind or None, stock status)
    for it, res in hits:
        s = stock.get(it["id"])
        if s is not None and s.get("price") and s["price"] > it["price"] * 1.01:
            quiet += 1  # price already corrected
            continue
        status = "out" if s is not None and s["in_stock"] == 0 else "in"
        kind = "new" if status == "in" else ("sold_out" if notify_sold_out else None)
        if kind is None:
            quiet += 1
        plan.append((it, res, kind, status))
    to_send = [p for p in plan if p[2]]
    to_send.sort(key=lambda p: p[2] == "sold_out")  # buyable ones first; stable, so biggest saving first

    no_ref = sum(1 for pid in items if not any(refs[n].get(pid) for n in refs))
    print(f"[{slug}] {len(items)} items, {no_ref} with no reference price, {count[0]} requests; "
          + ", ".join(f"1 {ref_cur[n] or n} = {r:.2f} {monitor.get('currency', 'SEK')}" for n, r in ratios.items())
          + f"; {len(hits) + known} price errors, {len(to_send)} to alert, {known} already reported")

    cap = int(sc.get("max_alerts_per_run", 10))
    if dry_run:
        print()
        print(f"{len(to_send)} item(s) would be alerted.")
        for n, (it, res, kind, _) in enumerate(to_send, 1):
            print_hit(n, it, res, stock.get(it["id"]), ref_cur)
            print(f"    alert type: {kind.replace('_', ' ')}")
        return False

    # Anything over the per-run cap is left unmarked and sent next run.
    for it, res, kind, status in to_send[:cap]:
        key = str(it["id"])
        try:
            ok = send_discord(webhook, userName="PricePilot",
                              content=(monitor.get("mention_on_bug") or None) if kind != "sold_out" else None,
                              embed=scan_embed(it, res, stock.get(it["id"]), ref_cur, monitor["name"], kind))
        except Exception as e:
            print(f"[{slug}] discord error: {type(e).__name__}")
            ok = False
        # Undelivered alerts are not marked, so they are sent again next run.
        if ok:
            alerted[key] = [it["price"], status, today]
        else:
            UNDELIVERED.append(it["id"])
        time.sleep(0.5)

    # Forget cached lookups soon after an item leaves the scan, and alerts only after
    # a long time, so a product that drops out and returns is not reported twice.
    for store, days in ((alerted, int(sc.get("alert_memory_days", 180))), (cache, int(sc.get("keep_days", 14)))):
        for k in list(store):
            if int(k) in items:
                store[k][-1] = today
            elif today - store[k][-1] > days:
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
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")  # Windows consoles choke on some product names

    cfg = load_cfg()
    webhook = os.getenv("DISCORD_WEBHOOK") or ""
    if not webhook and not (args.dry_run or args.from_dump):
        print("DISCORD_WEBHOOK is not set; refusing to run (use --dry-run to test).")
        sys.exit(1)

    if webhook:
        problem = check_webhook(webhook)
        if problem:
            print(f"DISCORD_WEBHOOK is wrong: {problem}. Nothing was scanned.")
            sys.exit(1)
        print("Discord webhook OK.")

    failed = False
    for m in cfg["monitors"]:
        print(f"→ Running: {m['name']}")
        result = run_scan_monitor(cfg, m, webhook, dry_run=args.dry_run, from_dump=args.from_dump)
        failed = failed or result is None
    if UNDELIVERED:
        print(f"{len(UNDELIVERED)} alert(s) could not be delivered to Discord; they will be retried next run.")
    sys.exit(1 if failed or UNDELIVERED else 0)


if __name__ == "__main__":
    main()
