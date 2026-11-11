from typing import Dict, Iterable, Optional
import httpx

# Say what we are. Claiming to be Chrome gets the API request refused.
UA = "PricePilot/2.0 (+https://github.com/Elieez/PricePilot-v2)"


class AsosApiAdapter:
    """ASOS product APIs (the JSON the site's own pages load).

    scan():          the search API, up to 200 products per request with brand and
                     price, so a whole category is read in ~20 requests.
    stock_prices():  price and per-size stock for specific product ids, in any
                     store, including products that store does not list in the
                     scanned category.

    Product ids are the same in every ASOS store, which is what makes comparing
    one store's price with another's possible.
    """
    BASE = "https://www.asos.com"
    SEARCH = BASE + "/api/product/search/v2/categories/{cid}"
    STOCKPRICE = BASE + "/api/product/catalogue/v4/stockprice"
    PAGE = 200
    LOOKUP_BATCH = 50

    def __init__(self, cfg: Dict, client: Optional[httpx.Client] = None, pause=None):
        self.cfg = cfg
        self.client = client or httpx.Client(
            timeout=25, follow_redirects=True,
            headers={"User-Agent": UA, "Accept": "application/json"},
        )
        self.pause = pause or (lambda: None)  # called before every request
        self.store = cfg.get("store", "SE")
        self.currency = cfg.get("currency", "SEK")
        self.requests = 0

    def _get(self, url: str, params: Dict):
        self.pause()
        self.requests += 1
        r = self.client.get(url, params=params)
        r.raise_for_status()
        return r.json()

    def _parse(self, p: Dict) -> Dict:
        price = p.get("price") or {}
        val = lambda k: (price.get(k) or {}).get("value")
        img = p.get("imageUrl") or ""
        return {
            "id": p["id"],
            "title": p.get("name"),
            "brand": p.get("brandName"),
            "price": val("current"),
            "previous": val("previous"),
            "currency": price.get("currency") or self.currency,
            "url": f"{self.BASE}/{self.store.lower()}/{(p.get('url') or '').lstrip('/')}",
            "image_url": ("https://" + img) if img and not img.startswith("http") else img,
        }

    def scan(self) -> Dict[int, Dict]:
        """Return {product_id: item} for the whole category."""
        items, offset = {}, 0
        while True:
            data = self._get(self.SEARCH.format(cid=self.cfg["category_id"]), {
                "offset": offset, "limit": self.PAGE, "sort": "freshness",
                "store": self.store, "country": self.cfg.get("country", self.store),
                "lang": self.cfg.get("lang", "sv-SE"), "currency": self.currency,
                "channel": "desktop-web",
            })
            products = data.get("products") or []
            for p in products:
                items.setdefault(p["id"], self._parse(p))
            offset += len(products)
            if not products or offset >= (data.get("itemCount") or 0):
                break
        return items

    def stock_prices(self, ids: Iterable[int]) -> Dict[int, Dict]:
        """Return {id: {"price", "in_stock", "sizes"}} for the ids this store sells.
        Ids the store does not carry are simply absent from the result."""
        ids, out = list(ids), {}
        for i in range(0, len(ids), self.LOOKUP_BATCH):
            data = self._get(self.STOCKPRICE, {
                "productIds": ",".join(str(x) for x in ids[i:i + self.LOOKUP_BATCH]),
                "store": self.store, "currency": self.currency,
            })
            for p in data or []:
                variants = p.get("variants") or []
                out[p["productId"]] = {
                    "price": ((p.get("productPrice") or {}).get("current") or {}).get("value"),
                    "in_stock": sum(1 for v in variants if v.get("isInStock")),
                    "sizes": len(variants),
                }
        return out


ADAPTERS = {
    "asos_api": AsosApiAdapter,
}
