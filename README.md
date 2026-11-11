# PricePilot

Scheduled GitHub Actions monitor that scans an ASOS Sweden category and posts
likely **price errors** to Discord. Built to send few alerts, almost all real.

## How a price error is detected

ASOS sells the same product, with the same product id, in every store. Each run:

1. scans the whole category in the Swedish store (about 20 requests for ~4,000 items),
2. scans the same category in the reference stores (GB and DE),
3. looks up by id the items a reference store sells but does not list in that
   category, and caches the answer until the Swedish price changes,
4. measures the normal SEK/GBP and SEK/EUR price ratio as the median over all
   items in the run (no FX API involved),
5. flags items whose Swedish price is at most `error_ratio` (50%) of what
   **every** reference store implies and at least `min_saving` SEK below it,
6. checks stock for the flagged items only.

Real example: a Samba at 110 GBP in the UK was listed at 110 SEK in Sweden,
0.07 of the expected ratio. The lowest normally priced item in the same scan
was at 0.70, so the threshold has a lot of room on both sides.

Requiring every reference store to agree means a wrong price in one of them
cannot cause an alert. Items no reference store sells (5 of 4,100) are not judged.

Each error is reported once: red if it can be bought, grey "SOLD OUT" if not
(turn off with `alert_sold_out: false`). When a known error comes back in stock
a green "Back in stock" alert follows. Undelivered alerts are retried on the next
run. A failed or partial scan sends nothing and leaves `state/` untouched.

## Setup

1. Discord: channel → Edit Channel → Integrations → Webhooks → New Webhook → copy URL.
2. GitHub: repo → Settings → Secrets and variables → Actions → New repository
   secret, name `DISCORD_WEBHOOK`, value = the webhook URL.
3. Actions tab → PricePilot Monitor → Run workflow.

The schedule is in `.github/workflows/monitor.yml`. A blocked or failed scan
fails the job, so it shows up red.

## Local use

```sh
pip install -r requirements.txt
python monitor.py --dry-run     # full scan (~3 min), prints what would be alerted, sends and saves nothing
python monitor.py --from-dump   # re-scores that scan instantly after a config change
python -m unittest discover tests
```

A real local run needs `DISCORD_WEBHOOK` in the environment.

## Configuration (`config.yaml`)

- `category_id`: the ASOS category to scan. Add another monitor block (with its
  own `slug`) to scan more categories.
- `reference_stores`: stores to compare against.
- `scoring`: `error_ratio`, `min_saving`, `max_alerts_per_run`.
- `filters.exclude_brands`: brands never alerted.

## Being a polite client

Requests identify themselves with an honest User-Agent, are paced with a pause
between each one, and run one scan at a time. Do not change that.
