# PricePilot

**PricePilot** is an automated price monitoring and notification tool, primarily written in Python. It is designed to track product prices on e-commerce sites, detect price changes and discounts, and send alerts (for example, via Discord webhooks). The project is easily extensible for new shops and supports multi-currency price tracking.

## Features

- **Automated Price Monitoring:** Periodically checks prices for specified products from supported online retailers.
- **Multi-Currency Support:** Fetches and caches foreign exchange rates to normalize prices (e.g., to SEK, EUR, USD, GBP).
- **Shop Adapters:** Extensible adapter framework for scraping and parsing product listings and details from different shops (e.g., ASOS, sites with static HTML/CSS).
- **Discount & Brand Filters:** Customizable filtering to alert only on specific brands or minimum discount thresholds.
- **State Management:** Remembers seen products and price state to avoid duplicate alerts.
- **Discord Notifications:** Sends rich, embeddable notifications to Discord channels via webhooks.

## Getting Started

### Requirements

- Python 3.8+
- Dependencies: `httpx`, `PyYAML`, `beautifulsoup4`, `lxml` (see `requirements.txt` if present)

### Setup

1. **Clone the Repository**
   ```sh
   git clone https://github.com/Elieez/PricePilot-v2.git
   cd PricePilot-v2
   ```

2. **Install Dependencies**
   ```sh
   pip install -r requirements.txt
   ```

3. **Discord Webhook Setup**

   In Discord, open the text channel where you want alerts → Edit Channel → Integrations → Webhooks → New Webhook → copy the webhook URL.

   **GitHub Actions secret (recommended):**  
   Repo → Settings → Secrets and variables → Actions → New repository secret

   - Name: `DISCORD_WEBHOOK`
   - Value: (paste the webhook URL)

   **Local runs (optional):** set it as an env var (avoid committing secrets):

   **macOS/Linux:**
   ```sh
   export DISCORD_WEBHOOK="https://discord.com/api/webhooks/..."
   ```

   **Windows PowerShell:**
   ```powershell
   $env:DISCORD_WEBHOOK = "https://discord.com/api/webhooks/..."
   ```

4. **Configure Monitors**
   - Edit or create a configuration file (YAML or JSON) specifying:
     - The shops to monitor
     - Webhook URLs
     - Filters for brands, discounts, etc.

5. **Run the Monitor**
   ```sh
   python monitor.py
   ```

## Usage

- The main entry point is `monitor.py`, which loads configuration, periodically fetches product listings, and sends notifications if price drops or new discounts are detected.
- Results are cached in the `state/` directory to avoid duplicate alerts and speed up repeated runs.

## How deals are picked (`asos_api` monitors)

Each run scans the whole category through the ASOS search API (about 20 requests for ~4,000 items) and compares every item with a reference price:

1. its own price on the previous run (catches silent drops on existing products),
2. the list price ASOS declares, if any,
3. otherwise the median price of the same brand + product type, falling back to the product type scaled by how expensive the brand usually is.

An item is alerted when it costs at most `deal_ratio` of that reference and saves at least `min_saving`. At `bug_ratio` or below it is flagged as a possible price error. Thresholds live under `scoring:` in `config.yaml`.

Tune them without sending anything:

```sh
python monitor.py --dry-run
```

Tests: `python -m unittest discover tests`

## Extending

- Add new shop support by creating an adapter in `adapters.py` that implements `discover_urls` (to find products) and `fetch_offer` (to extract price, brand, etc.).
- Adjust notification logic or add new output channels as needed.

## Technologies Used

- **Python:** Main application language
- **httpx:** For HTTP requests
- **PyYAML:** For flexible config parsing
- **BeautifulSoup & lxml:** HTML parsing
- **Discord Webhooks:** For alert notifications

---

*Repository: [Elieez/PricePilot-v2](https://github.com/Elieez/PricePilot-v2)*
