#!/usr/bin/env python3
"""Harbor Freight price watcher - sends email alerts when prices drop below thresholds."""

import json
import os
import random
import re
import smtplib
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

WATCHLIST_FILE = Path(__file__).parent / "watchlist.json"
STATE_FILE = Path(__file__).parent / "last_state.json"

# Rotate through different browser profiles to avoid detection
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

MAX_RETRIES = 3
RETRY_DELAY_BASE = 5  # seconds


def get_config():
    """Load configuration from environment variables."""
    return {
        "emails": [e.strip() for e in os.environ.get("EMAIL_RECIPIENTS", "").split(",") if e.strip()],
        "smtp_user": os.environ.get("SMTP_USER"),
        "smtp_pass": os.environ.get("SMTP_PASS"),
    }


def load_watchlist() -> list[dict]:
    """Load items to watch from config file."""
    if not WATCHLIST_FILE.exists():
        print(f"Error: {WATCHLIST_FILE} not found")
        sys.exit(1)
    with open(WATCHLIST_FILE) as f:
        data = json.load(f)
    return data.get("items", [])


def load_previous_state() -> dict:
    """Load previous price state."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    """Save current price state."""
    state["updated_at"] = datetime.now().isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def extract_sku_from_url(url: str) -> str | None:
    """Extract SKU from Harbor Freight URL."""
    match = re.search(r"-(\d+)\.html$", url)
    return match.group(1) if match else None


def get_headers() -> dict:
    """Get randomized headers to avoid bot detection."""
    ua = random.choice(USER_AGENTS)
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Ch-Ua": '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"' if "Windows" in ua else '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def parse_price_from_html(html: str, url: str) -> dict:
    """Parse product info from HTML response."""
    if "PerimeterX" in html or "px-captcha" in html:
        return {"error": "Blocked by bot protection"}

    # Extract JSON-LD Product data
    pattern = r'<script type="application/ld\+json">\s*(\{[^<]*"@type"\s*:\s*"Product"[^<]*\})\s*</script>'
    matches = re.findall(pattern, html, re.DOTALL)

    for match in matches:
        try:
            data = json.loads(match)
            if data.get("@type") == "Product":
                offers = data.get("offers", {})
                return {
                    "name": data.get("name"),
                    "sku": data.get("sku"),
                    "price": float(offers.get("price", 0)),
                    "availability": offers.get("availability", ""),
                }
        except (json.JSONDecodeError, ValueError):
            continue

    # Fallback: try og:price:amount meta tag
    og_price = re.search(r'og:price:amount"\s+content="([^"]+)"', html)
    og_name = re.search(r'og:title"\s+content="([^"]+)"', html)
    if og_price:
        return {
            "name": og_name.group(1) if og_name else "Unknown",
            "sku": extract_sku_from_url(url),
            "price": float(og_price.group(1)),
            "availability": "unknown",
        }

    return {"error": "Could not parse price from page"}


def fetch_price(url: str) -> dict:
    """Fetch product info from Harbor Freight with retries."""
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            if attempt > 0:
                delay = RETRY_DELAY_BASE * (2 ** (attempt - 1)) + random.uniform(0, 2)
                print(f"  Retry {attempt}/{MAX_RETRIES - 1} after {delay:.1f}s...")
                time.sleep(delay)

            headers = get_headers()
            response = requests.get(url, headers=headers, timeout=30)

            # If we get a 403, retry with different headers
            if response.status_code == 403:
                last_error = "403 Forbidden"
                continue

            response.raise_for_status()
            result = parse_price_from_html(response.text, url)

            # If blocked by PerimeterX, retry
            if "error" in result and "bot protection" in result["error"]:
                last_error = result["error"]
                continue

            return result

        except requests.RequestException as e:
            last_error = str(e)
            continue

    return {"error": f"Failed after {MAX_RETRIES} attempts: {last_error}"}


def check_prices(items: list[dict], previous_state: dict) -> tuple[list[dict], dict]:
    """Check prices for all items, return alerts and new state."""
    alerts = []
    new_state = {"prices": {}}

    for i, item in enumerate(items):
        # Add delay between items to look more natural
        if i > 0:
            delay = random.uniform(2, 5)
            time.sleep(delay)
        url = item["url"]
        threshold = item.get("threshold")
        name = item.get("name", "Unknown Item")
        sku = extract_sku_from_url(url) or "unknown"

        print(f"Checking: {name} (SKU: {sku})")

        result = fetch_price(url)

        if "error" in result:
            print(f"  Error: {result['error']}")
            # Keep previous price in state if fetch failed
            if sku in previous_state.get("prices", {}):
                new_state["prices"][sku] = previous_state["prices"][sku]
            continue

        current_price = result["price"]
        previous_price = previous_state.get("prices", {}).get(sku, {}).get("price")

        print(f"  Price: ${current_price:.2f} (threshold: ${threshold:.2f})")

        new_state["prices"][sku] = {
            "price": current_price,
            "name": result["name"],
            "url": url,
            "last_checked": datetime.now().isoformat(),
        }

        # Alert if price is at or below threshold
        if threshold and current_price <= threshold:
            # Only alert if this is a new drop (wasn't already below threshold)
            was_below = previous_price is not None and previous_price <= threshold
            if not was_below:
                alerts.append({
                    "name": result["name"] or name,
                    "sku": sku,
                    "price": current_price,
                    "threshold": threshold,
                    "previous_price": previous_price,
                    "url": url,
                })
                print(f"  ALERT: Price ${current_price:.2f} is at or below threshold ${threshold:.2f}!")

    return alerts, new_state


def format_email_html(alerts: list[dict]) -> str:
    """Generate HTML email body."""
    rows = "\n".join(
        f"""<tr>
            <td style="padding:12px;border-bottom:1px solid #ddd;">{a['name']}</td>
            <td style="padding:12px;border-bottom:1px solid #ddd;color:#16a34a;font-weight:bold;">${a['price']:.2f}</td>
            <td style="padding:12px;border-bottom:1px solid #ddd;">${a['threshold']:.2f}</td>
            <td style="padding:12px;border-bottom:1px solid #ddd;">
                {"${:.2f}".format(a['previous_price']) if a['previous_price'] else "N/A"}
            </td>
            <td style="padding:12px;border-bottom:1px solid #ddd;">
                <a href="{a['url']}" style="color:#dc2626;font-weight:bold;">Buy Now</a>
            </td>
        </tr>"""
        for a in alerts
    )
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:system-ui,sans-serif;max-width:800px;margin:0 auto;padding:20px;">
    <h2 style="color:#dc2626;">Harbor Freight Price Alert!</h2>
    <p>The following item(s) have dropped to or below your target price:</p>
    <table style="border-collapse:collapse;width:100%;margin:20px 0;">
        <thead>
            <tr style="background:#f3f4f6;">
                <th style="padding:12px;text-align:left;border-bottom:2px solid #ddd;">Item</th>
                <th style="padding:12px;text-align:left;border-bottom:2px solid #ddd;">Current Price</th>
                <th style="padding:12px;text-align:left;border-bottom:2px solid #ddd;">Your Threshold</th>
                <th style="padding:12px;text-align:left;border-bottom:2px solid #ddd;">Previous Price</th>
                <th style="padding:12px;text-align:left;border-bottom:2px solid #ddd;">Action</th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
    </table>
    <p style="color:#666;font-size:14px;">
        Prices may change - act fast!
    </p>
</body>
</html>"""


def format_email_text(alerts: list[dict]) -> str:
    """Generate plain text email body."""
    lines = ["Harbor Freight Price Alert!", "", f"Found {len(alerts)} item(s) at or below your target price:", ""]
    for a in alerts:
        lines.append(f"• {a['name']}")
        lines.append(f"  Current: ${a['price']:.2f} (threshold: ${a['threshold']:.2f})")
        lines.append(f"  Link: {a['url']}")
        lines.append("")
    return "\n".join(lines)


def send_email(recipients: list[str], alerts: list[dict], smtp_user: str, smtp_pass: str):
    """Send notification email via Gmail SMTP."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Harbor Freight: {len(alerts)} item(s) hit your price target!"
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)

    msg.attach(MIMEText(format_email_text(alerts), "plain"))
    msg.attach(MIMEText(format_email_html(alerts), "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(smtp_user, smtp_pass.replace("\xa0", " "))
        server.sendmail(smtp_user, recipients, msg.as_string())
    print(f"Email sent to {', '.join(recipients)}")


def main():
    config = get_config()

    if not config["emails"]:
        print("Warning: EMAIL_RECIPIENTS not set - will print results only")

    print("Loading watchlist...")
    items = load_watchlist()
    print(f"Found {len(items)} item(s) to check")

    if not items:
        print("No items in watchlist")
        return

    previous_state = load_previous_state()
    alerts, new_state = check_prices(items, previous_state)

    # Always save state
    save_state(new_state)
    print("State saved.")

    if not alerts:
        print("No price alerts to send.")
        return

    print(f"\n{len(alerts)} alert(s) to send!")

    if not config["smtp_user"] or not config["smtp_pass"]:
        print("SMTP credentials not configured - printing alerts only:")
        for a in alerts:
            print(f"  {a['name']}: ${a['price']:.2f} (threshold: ${a['threshold']:.2f})")
            print(f"    {a['url']}")
        return

    send_email(config["emails"], alerts, config["smtp_user"], config["smtp_pass"])


if __name__ == "__main__":
    main()
