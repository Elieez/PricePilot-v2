"""End-to-end check of the cross-store scan against a fake ASOS API.

Prices are real values from Nov 2026: the Samba that was 110 GBP in the UK and
110 SEK in Sweden, plus the four false alerts the old sibling comparison raised.
Run with:  python -m unittest discover tests
"""
import json, sys, tempfile, unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import adapters, monitor

GBP, EUR = 14.25, 11.5
ALL = ("SE", "COM", "DE")

# name: (brand, SEK, GBP, EUR, stores that list it in the category, stores that sell it, sizes in stock in SE)
SPECIAL = {
    "samba":     ("adidas Originals", 110, 110, 110, ALL, ALL, 1),             # GBP entered as SEK
    "samba_og":  ("adidas Originals", 1449, 100, 120, ALL, ALL, 5),
    # the old false alerts: cheaper kinds of product, priced normally everywhere
    "keyring":   ("Dr Martens", 289, 20, 25, ALL, ALL, 1),
    "shacket":   ("Polo Ralph Lauren", 2839, 200, 215, ("SE",), ALL, 4),       # only found by id lookup
    "lounge":    ("Polo Ralph Lauren", 1639, 115, 125, ALL, ALL, 4),
    "tee":       ("adidas performance", 349, 23, 30, ("SE",), ALL, 4),
    # wrong (too high) price in ONE reference store must not alert
    "gb_wrong":  ("Nike", 1399, 980, 120, ALL, ALL, 5),
    # real errors
    "unlisted":  ("Salomon", 129, 129, 149, ("SE",), ALL, 3),                  # reference only via id lookup
    "one_store": ("New Balance", 99, None, 120, ("SE", "DE"), ("SE", "DE"), 2), # sold in one reference store only
    "sold_out":  ("Asics", 150, 150, 170, ALL, ALL, 0),                        # error, but nothing to buy
    # error-priced, but nothing to compare with / too small to matter
    "se_only":   ("Vans", 80, None, None, ("SE",), ("SE",), 5),
    "cheap":     ("Nike", 20, 15, 18, ALL, ALL, 5),                            # saving under min_saving
    "excluded":  ("ASOS DESIGN", 60, 60, 70, ALL, ALL, 5),                     # brand on the exclude list
}
NAMES = list(SPECIAL)
PID = {n: 2000 + i for i, n in enumerate(NAMES)}


class FakeAsos:
    def __init__(self):
        self.rows = {}  # id -> dict
        for i in range(60):  # ordinary items at ordinary ratios
            sek = 300 + 40 * i
            self._add(1000 + i, "Brand", sek, round(sek / GBP, 2), round(sek / EUR, 2), ALL, ALL, 5)
        for n, spec in SPECIAL.items():
            self._add(PID[n], *spec)
        self.calls = 0
        self.fail = None  # predicate(path, query) -> True to answer 403

    def _add(self, pid, brand, sek, gbp, eur, listed, sold, in_stock):
        self.rows[pid] = {"brand": brand, "price": {"SE": sek, "COM": gbp, "DE": eur},
                          "listed": set(listed), "sold": set(sold), "in_stock": in_stock}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        url = urlsplit(str(request.url))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        store = q["store"]
        if self.fail and self.fail(url.path, q):
            return httpx.Response(403)
        if url.path.endswith("/stockprice"):
            out = []
            for pid in map(int, q["productIds"].split(",")):
                r = self.rows.get(pid)
                if r and store in r["sold"]:
                    n = r["in_stock"] if store == "SE" else 5
                    out.append({"productId": pid, "productPrice": {"current": {"value": r["price"][store]}},
                                "variants": [{"isInStock": i < n} for i in range(5)]})
            return httpx.Response(200, json=out)
        rows = [(pid, r) for pid, r in self.rows.items() if store in r["listed"]]
        off, lim = int(q["offset"]), int(q["limit"])
        products = [{"id": pid, "name": f"{r['brand']} item {pid}", "brandName": r["brand"],
                     "url": f"x/prd/{pid}", "imageUrl": "images.asos-media.com/x",
                     "price": {"current": {"value": float(r["price"][store])}, "previous": {"value": None},
                               "currency": q["currency"]}}
                    for pid, r in rows[off:off + lim]]
        return httpx.Response(200, json={"itemCount": len(rows), "products": products})


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        monitor.STATE_DIR = Path(self.tmp.name)
        monitor.DUMP_FILE = Path(self.tmp.name) / "scan-dump.json"
        self.api = FakeAsos()
        self.sent = []
        monitor.send_discord = lambda webhook, **kw: self.sent.append(kw) or True
        monitor.time.sleep = lambda s: None
        client = httpx.Client(transport=httpx.MockTransport(lambda r: self.api.handler(r)))
        real = adapters.AsosApiAdapter
        real.PAGE = 20  # force pagination
        monitor.ADAPTERS["asos_api"] = lambda cfg, pause=None: real(cfg, client=client, pause=pause)
        self.mon = {"name": "t", "slug": "t", "adapter": "asos_api", "category_id": 27110,
                    "filters": {"exclude_brands": ["asos design"]}}
        self.cfg = {}
        self.state_file = monitor.STATE_DIR / "t.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, **kw):
        self.sent.clear()
        return monitor.run_scan_monitor(self.cfg, self.mon, "hook", **kw)

    def alerted_ids(self):
        return [int(m["embed"]["url"].rsplit("/", 1)[1]) for m in self.sent]

    def test_flow(self):
        # dry run: nothing sent, nothing saved
        self.assertFalse(self.run_once(dry_run=True))
        self.assertEqual(self.sent, [])
        self.assertFalse(self.state_file.exists())
        dump = json.loads(monitor.DUMP_FILE.read_text(encoding="utf-8"))["t"]
        self.assertEqual(set(dump["refs"]), {"GB", "DE"})
        self.assertEqual(dump["refs"]["GB"][str(PID["shacket"])], 200)  # id lookup is in the dump

        # re-scoring the saved scan needs no requests at all
        before = self.api.calls
        self.assertFalse(self.run_once(from_dump=True))
        self.assertEqual(self.api.calls, before)

        # run 1: the three in-stock price errors, biggest saving first; none of the old false alerts
        self.assertTrue(self.run_once())
        self.assertEqual(self.alerted_ids(), [PID["unlisted"], PID["one_store"], PID["samba"]])
        samba = self.sent[2]["embed"]
        self.assertIn("Possible price error", samba["title"])
        self.assertTrue(samba["url"].startswith("https://www.asos.com/se/"))
        self.assertIn("1 of 5 sizes in stock", samba["description"])
        self.assertIn("GB: 110 GBP", samba["description"])
        state = json.loads(self.state_file.read_text())
        self.assertEqual(set(state["alerted"]), {str(PID[n]) for n in ("samba", "unlisted", "one_store")})
        self.assertEqual(state["refs"][str(PID["tee"])][1], {"GB": 23, "DE": 30})
        self.assertEqual(state["refs"][str(PID["se_only"])][1], {"GB": None, "DE": None})

        # run 2: nothing changed -> no alert, no state write; lookups come from the cache,
        # so only the three category scans plus the stock check for the sold-out hit
        before = self.api.calls
        self.assertFalse(self.run_once())
        self.assertEqual(self.sent, [])
        pages = lambda store: -(-sum(store in r["listed"] for r in self.api.rows.values()) // 20)
        self.assertEqual(self.api.calls - before, pages("SE") + pages("COM") + pages("DE") + 1)

        # run 3: the sold-out error is restocked -> alerted now
        self.api.rows[PID["sold_out"]]["in_stock"] = 2
        self.assertTrue(self.run_once())
        self.assertEqual(self.alerted_ids(), [PID["sold_out"]])

        # run 4: an item known only through a cached lookup gets a wrong price -> looked up again, alerted
        self.api.rows[PID["tee"]]["price"]["SE"] = 23
        self.api.rows[PID["tee"]]["price"]["COM"] = 60  # reference moved too: the cache must not be trusted
        self.assertTrue(self.run_once())
        self.assertEqual(self.alerted_ids(), [PID["tee"]])
        self.assertIn("GB: 60 GBP", self.sent[0]["embed"]["description"])

        # run 5: same prices -> not repeated
        self.assertFalse(self.run_once())
        self.assertEqual(self.sent, [])

    def test_include_sold_out_sends_it_marked_and_keeps_it_retryable(self):
        self.run_once(include_sold_out=True)
        titles = {i: m["embed"]["title"] for i, m in zip(self.alerted_ids(), self.sent)}
        self.assertEqual(len(titles), 4)
        self.assertIn("SOLD OUT", titles[PID["sold_out"]])
        self.assertIn("currency mix-up", self.sent[0]["embed"]["description"])
        self.assertNotIn(str(PID["sold_out"]), json.loads(self.state_file.read_text())["alerted"])

    def test_undelivered_alert_is_retried(self):
        monitor.send_discord = lambda webhook, **kw: False
        self.run_once()
        monitor.send_discord = lambda webhook, **kw: self.sent.append(kw) or True
        self.run_once()
        self.assertEqual(len(self.sent), 3)

    def check_failure_changes_nothing(self, fail):
        self.assertTrue(self.run_once())
        saved = self.state_file.read_text()
        self.api.rows[PID["samba_og"]]["price"]["SE"] = 100  # a new error that must NOT get through
        self.api.fail = fail
        self.assertIsNone(self.run_once())
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state_file.read_text(), saved)

    def test_failed_scan_changes_nothing(self):
        self.check_failure_changes_nothing(lambda path, q: True)

    def test_partial_scan_changes_nothing(self):
        self.check_failure_changes_nothing(lambda path, q: q["store"] == "SE" and q.get("offset") == "40")

    def test_failed_reference_store_changes_nothing(self):
        self.check_failure_changes_nothing(lambda path, q: q["store"] == "DE")

    def test_failed_stock_check_changes_nothing(self):
        self.check_failure_changes_nothing(lambda path, q: path.endswith("/stockprice") and q["store"] == "SE")

    def test_too_few_matches_is_a_failure(self):
        for r in self.api.rows.values():
            r["listed"].discard("COM"); r["sold"].discard("COM")
        self.api._add(1, "Brand", 500, 35, 43, ALL, ALL, 5)
        self.assertIsNone(self.run_once())
        self.assertFalse(self.state_file.exists())


if __name__ == "__main__":
    unittest.main()
