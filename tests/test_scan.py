"""End-to-end check of the scan monitor against a fake ASOS search API.

Sneaker brands/prices are real values from the SE new-in category (Nov 2026);
the 110 SEK Samba is the price error that the old monitor happened to catch.
Run with:  python -m unittest discover tests
"""
import json, sys, tempfile, unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import adapters, monitor

SNEAKERS, TSHIRTS, HATS = 8606, 8415, 8238
CATALOGUE = [  # (brand, type, price)
    *[("adidas Originals", SNEAKERS, p) for p in (1549, 1649, 1349, 1749, 1749, 1749, 1349, 1449, 1449, 1349, 1349, 1549)],
    *[("Nike", SNEAKERS, p) for p in (1399, 999, 1149, 1499, 1499, 1499, 1149, 1499, 1399, 1399, 1399, 2049, 1599)],
    *[("Vans", SNEAKERS, p) for p in (809, 929, 1889, 1339, 879)],
    *[("ASOS DESIGN", SNEAKERS, p) for p in (539, 499, 499, 639, 639)],
    *[("Pull&Bear", SNEAKERS, p) for p in (379, 379)],               # rare + cheap brand: not a deal
    ("Salomon", SNEAKERS, 129),                                     # rare brand, absurd price: still caught
    *[("ASOS DESIGN", TSHIRTS, 119)] * 9,
    *[("Nike", TSHIRTS, p) for p in (349, 399, 449, 399, 159)],     # 159: cheap basic, saving too small
    *[("Hugo Red", TSHIRTS, p) for p in (559, 559, 649)],
    *[("Reclaimed Vintage", HATS, 99)] * 3,
    *[("ASOS DESIGN", HATS, p) for p in (119, 119, 149, 179)],
    ("The North Face", HATS, 349),
    ("adidas Originals", SNEAKERS, 110),                            # the Samba
]
SAMBA_ID = 1000 + len(CATALOGUE) - 1


class FakeAsos:
    def __init__(self):
        self.products = [
            {"id": 1000 + i, "name": f"{b} item {i}", "brandName": b, "_type": t,
             "url": f"{b.lower().replace(' ', '-')}/x/prd/{1000 + i}", "imageUrl": "images.asos-media.com/x",
             "price": {"current": {"value": float(p)}, "previous": {"value": None},
                       "rrp": {"value": None}, "currency": "SEK"}}
            for i, (b, t, p) in enumerate(CATALOGUE)
        ]
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        q = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        rows = self.products
        if "attribute_1047" in q:
            rows = [p for p in rows if p["_type"] == int(q["attribute_1047"])]
        off, lim = int(q["offset"]), int(q["limit"])
        facets = [{"id": "attribute_1047", "facetValues": [
            {"id": t, "name": n, "count": sum(p["_type"] == t for p in self.products)}
            for t, n in ((SNEAKERS, "Träningsskor & sneakers"), (TSHIRTS, "T-shirts"), (HATS, "Mössor"))]}]
        return httpx.Response(200, json={"itemCount": len(rows), "products": rows[off:off + lim], "facets": facets})


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        monitor.STATE_DIR = Path(self.tmp.name)
        self.api = FakeAsos()
        self.sent = []
        monitor.send_discord = lambda webhook, **kw: self.sent.append(kw) or True
        monitor.time.sleep = lambda s: None
        client = httpx.Client(transport=httpx.MockTransport(self.api.handler))
        real = adapters.AsosApiAdapter
        adapters.AsosApiAdapter.PAGE = 20  # force pagination
        monitor.ADAPTERS["asos_api"] = lambda cfg, pause=None: real(cfg, client=client)
        self.mon = {"name": "t", "slug": "t", "adapter": "asos_api", "category_id": 27110}
        self.cfg = {"filters": {"exclude_brands": []}}

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, **kw):
        self.sent.clear()
        return monitor.run_scan_monitor(self.cfg, self.mon, "hook", **kw)

    def test_flow(self):
        # dry run: nothing sent, nothing saved
        self.assertFalse(self.run_once(dry_run=True))
        self.assertEqual(self.sent, [])
        self.assertFalse((monitor.STATE_DIR / "t.json").exists())

        # run 1: the Samba and the Salomon are alerted as price errors; no junk
        self.assertTrue(self.run_once())
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all("Possible price error" in m["embed"]["title"] for m in self.sent))
        self.assertIn(f"/prd/{SAMBA_ID}", self.sent[0]["embed"]["url"])  # biggest saving first
        self.assertTrue(self.sent[0]["embed"]["url"].startswith("https://www.asos.com/se/"))
        state = json.loads((monitor.STATE_DIR / "t.json").read_text())
        self.assertEqual(len(state["items"]), len(CATALOGUE))
        self.assertTrue(all(v[1] for v in state["items"].values()))  # every item got a type

        # run 2: nothing changed -> no alert, no state write, and only the unfiltered scan is requested
        before = self.api.calls
        self.assertFalse(self.run_once())
        self.assertEqual(self.sent, [])
        self.assertEqual(self.api.calls - before, -(-len(CATALOGUE) // 20))

        # run 3: an existing Nike sneaker drops 1399 -> 700 (not marked as a sale by ASOS)
        nike = next(p for p in self.api.products if p["brandName"] == "Nike" and p["price"]["current"]["value"] == 1399)
        nike["price"]["current"]["value"] = 700.0
        self.assertTrue(self.run_once())
        self.assertEqual(len(self.sent), 1)
        self.assertIn(f"/prd/{nike['id']}", self.sent[0]["embed"]["url"])
        self.assertIn("previous check", self.sent[0]["embed"]["description"])

        # run 4: same prices -> not repeated
        self.assertFalse(self.run_once())
        self.assertEqual(self.sent, [])

        # run 5: a new product appears -> its type is resolved, state updates, no alert
        new = json.loads(json.dumps(self.api.products[0])); new["id"] = 9999
        self.api.products.insert(0, new)
        self.assertTrue(self.run_once())
        self.assertEqual(self.sent, [])
        state = json.loads((monitor.STATE_DIR / "t.json").read_text())
        self.assertEqual(state["items"]["9999"][1], SNEAKERS)

    def test_failed_scan_leaves_state_alone(self):
        self.assertTrue(self.run_once())
        saved = (monitor.STATE_DIR / "t.json").read_text()
        self.api.handler = None
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
        monitor.ADAPTERS["asos_api"] = lambda cfg, pause=None: adapters.AsosApiAdapter(cfg, client=client)
        self.assertFalse(self.run_once())
        self.assertEqual((monitor.STATE_DIR / "t.json").read_text(), saved)


if __name__ == "__main__":
    unittest.main()
