#!/usr/bin/env python3
"""
Price Monitor
=============
Checks one or more product page URLs for their current price, and emails you
when the price drops below a threshold (either an absolute target price or a
percent-off-baseline). Designed to be run once a day by cron or a GitHub
Actions scheduled workflow -- it is NOT a long-running process.

Usage:
    python3 price_monitor.py [--config config.json] [--state state.json]

See README.md for full setup instructions.
"""

import argparse
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Connection": "keep-alive",
}

FAILURE_ALERT_EVERY = 3  # send a "this check is broken" email every N consecutive failures
REQUEST_RETRIES = 2
REQUEST_RETRY_DELAY_SECONDS = 4

# One shared session so cookies persist across requests (and across products
# on the same domain within a single run), plus a memo of which domains
# we've already "warmed up" with a homepage visit this run.
_session = requests.Session()
_warmed_domains = set()


def _warm_up_domain(url):
    """
    Visit a site's homepage once before requesting a deep product URL, and
    remember that we did. Some bot-protection treats a request that jumps
    straight to a product page with no prior visit as a signal of automation;
    this mimics a normal browsing path instead. Best-effort only - failures
    here are not fatal, the real request is still attempted afterward.
    """
    parsed = urlparse(url)
    domain = f"{parsed.scheme}://{parsed.netloc}"
    if domain not in _warmed_domains:
        try:
            _session.get(domain, headers=HEADERS, timeout=15)
        except requests.RequestException:
            pass
        _warmed_domains.add(domain)
    return domain


# --------------------------------------------------------------------------
# Price extraction
# --------------------------------------------------------------------------

def _parse_dollar_amount(text):
    """Pull the first-looking-like-a-price number out of a string."""
    if not text:
        return None
    match = re.search(r"[\$£€]\s?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)", text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _extract_price_from_jsonld(item):
    if not isinstance(item, dict):
        return None
    offers = item.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if isinstance(offers, dict):
        price = offers.get("price") or offers.get("lowPrice")
        if price is not None:
            try:
                return float(str(price).replace(",", ""))
            except ValueError:
                pass
    graph = item.get("@graph")
    if isinstance(graph, list):
        for sub in graph:
            price = _extract_price_from_jsonld(sub)
            if price is not None:
                return price
    return None


def extract_price(html):
    """
    Try several strategies, in order of reliability, to find a product's
    current price in a page's HTML. Returns a float or None.
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1. Open Graph / product meta tags (Anthropologie, Urban Outfitters, Free
    #    People, and many other retail sites expose this for social sharing).
    for prop in ("product:price:amount", "og:price:amount"):
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            try:
                return float(tag["content"].replace(",", ""))
            except ValueError:
                pass

    # 2. schema.org JSON-LD (very common across e-commerce platforms)
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except (TypeError, ValueError):
            continue
        for item in (data if isinstance(data, list) else [data]):
            price = _extract_price_from_jsonld(item)
            if price is not None:
                return price

    # 3. itemprop="price" microdata (the "content" attribute, when present, is
    #    a plain machine-readable number with no currency symbol)
    tag = soup.find(attrs={"itemprop": "price"})
    if tag is not None:
        content_attr = tag.get("content")
        if content_attr:
            try:
                return float(content_attr.replace(",", ""))
            except ValueError:
                pass
        price = _parse_dollar_amount(tag.get_text())
        if price is not None:
            return price

    # 4. Last resort: first element whose class name contains "price"
    for tag in soup.find_all(class_=re.compile("price", re.I)):
        price = _parse_dollar_amount(tag.get_text())
        if price is not None:
            return price

    return None


def extract_title(html, fallback):
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("meta", attrs={"property": "og:title"})
    if tag and tag.get("content"):
        return tag["content"].strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return fallback


def fetch_page(url):
    last_error = None
    for attempt in range(1, REQUEST_RETRIES + 2):
        try:
            domain = _warm_up_domain(url)
            headers = dict(HEADERS)
            headers["Referer"] = domain + "/"
            headers["Sec-Fetch-Site"] = "same-origin"
            resp = _session.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            last_error = e
            if attempt <= REQUEST_RETRIES:
                time.sleep(REQUEST_RETRY_DELAY_SECONDS)
    raise last_error


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def send_email(smtp_cfg, subject, body):
    required = ("smtp_server", "sender_email", "sender_password", "recipient_email")
    missing = [k for k in required if not smtp_cfg.get(k)]
    if missing:
        raise RuntimeError(f"Missing email config/env values: {', '.join(missing)}")

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = smtp_cfg["sender_email"]
    msg["To"] = smtp_cfg["recipient_email"]

    with smtplib.SMTP(smtp_cfg["smtp_server"], int(smtp_cfg.get("smtp_port", 587))) as server:
        server.starttls()
        server.login(smtp_cfg["sender_email"], smtp_cfg["sender_password"])
        server.sendmail(smtp_cfg["sender_email"], [smtp_cfg["recipient_email"]], msg.as_string())


def resolve_email_config(config):
    """Config file values, overridable by environment variables (for CI secrets)."""
    cfg = dict(config.get("email", {}))
    env_map = {
        "smtp_server": "SMTP_SERVER",
        "smtp_port": "SMTP_PORT",
        "sender_email": "SENDER_EMAIL",
        "sender_password": "SENDER_PASSWORD",
        "recipient_email": "RECIPIENT_EMAIL",
    }
    for key, env_var in env_map.items():
        if os.environ.get(env_var):
            cfg[key] = os.environ[env_var]
    cfg.setdefault("smtp_port", 587)
    return cfg


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_product(product, product_state, log):
    url = product["url"]
    name = product.get("name") or url
    target_price = product.get("target_price")
    drop_percent = product.get("drop_percent")
    configured_baseline = product.get("baseline_price")

    if target_price is None and drop_percent is None:
        log(f"{name}: skipped - set either 'target_price' or 'drop_percent' in config.json")
        return None

    try:
        html = fetch_page(url)
    except Exception as e:
        product_state["consecutive_failures"] = product_state.get("consecutive_failures", 0) + 1
        product_state["last_checked"] = now_iso()
        product_state["last_error"] = f"request failed: {e}"
        log(f"{name}: ERROR fetching page - {e}")
        return None

    current_price = extract_price(html)
    if current_price is None:
        product_state["consecutive_failures"] = product_state.get("consecutive_failures", 0) + 1
        product_state["last_checked"] = now_iso()
        product_state["last_error"] = "price not found on page"
        log(f"{name}: could not find a price on the page (site layout may have changed)")
        return None

    if not product.get("name"):
        name = extract_title(html, fallback=name)

    product_state["consecutive_failures"] = 0
    product_state.pop("last_error", None)
    product_state["last_checked"] = now_iso()
    product_state["last_price"] = current_price

    baseline_price = configured_baseline or product_state.get("baseline_price") or current_price
    product_state["baseline_price"] = baseline_price

    threshold_price = target_price if target_price is not None else baseline_price * (1 - drop_percent / 100)

    log(f"{name}: ${current_price:.2f}  (baseline ${baseline_price:.2f}, threshold ${threshold_price:.2f})")

    last_alert_price = product_state.get("last_alert_price")
    should_alert = current_price <= threshold_price and (
        last_alert_price is None or current_price < last_alert_price
    )

    # If price recovered back above the threshold, clear the alert memory so a
    # future dip re-triggers a fresh alert instead of staying silent forever.
    if current_price > threshold_price and last_alert_price is not None:
        product_state.pop("last_alert_price", None)

    if not should_alert:
        return None

    pct_off = (1 - current_price / baseline_price) * 100 if baseline_price else 0
    subject = f"Price drop: {name} is now ${current_price:.2f}"
    body = (
        f"{name}\n{url}\n\n"
        f"Current price:  ${current_price:.2f}\n"
        f"Baseline price: ${baseline_price:.2f}\n"
        f"Drop:           {pct_off:.1f}%\n"
        f"Your threshold: ${threshold_price:.2f}\n"
    )
    product_state["last_alert_price"] = current_price
    product_state["last_alert_at"] = now_iso()
    return {"subject": subject, "body": body}


def find_product(config, url):
    for product in config.get("products", []):
        if product["url"] == url:
            return product
    return None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_check(args, config, state, log):
    smtp_cfg = resolve_email_config(config)

    alerts_to_send = []
    failure_alerts_to_send = []

    for product in config.get("products", []):
        url = product["url"]
        product_state = state.setdefault(url, {})
        alert = check_product(product, product_state, log)
        if alert:
            alerts_to_send.append(alert)

        failures = product_state.get("consecutive_failures", 0)
        if failures > 0 and failures % FAILURE_ALERT_EVERY == 0:
            name = product.get("name") or url
            failure_alerts_to_send.append({
                "subject": f"Price monitor is failing for {name}",
                "body": (
                    f"The price checker has failed {failures} times in a row for:\n"
                    f"{name}\n{url}\n\n"
                    f"Last error: {product_state.get('last_error')}\n\n"
                    f"The site may be blocking automated requests or have changed its "
                    f"page layout. You may need to update the script's price-parsing logic."
                ),
            })

    for alert in alerts_to_send + failure_alerts_to_send:
        try:
            send_email(smtp_cfg, alert["subject"], alert["body"])
            log(f"Sent email: {alert['subject']}")
        except Exception as e:
            log(f"FAILED to send email ({alert['subject']}): {e}")

    config_changed = False
    return config_changed, True  # state always worth saving after a check


def cmd_add(args, config, state, log):
    url = args.url or input("Product URL: ").strip()

    target_price = args.target_price
    drop_percent = args.drop_percent
    if target_price is None and drop_percent is None:
        choice = input(
            "Alert when the price is (1) at or below a specific dollar amount, "
            "or (2) a percent off? [1/2]: "
        ).strip()
        if choice == "2":
            drop_percent = float(input("Percent off to trigger an alert (e.g. 20 for 20%): ").strip())
        else:
            target_price = float(input("Alert me once the price is at or below $: ").strip())

    name = args.name
    if name is None and sys.stdin.isatty():
        name_input = input("Name for this product (optional, press Enter to skip): ").strip()
        name = name_input or None

    product = {"url": url}
    if name:
        product["name"] = name
    if target_price is not None:
        product["target_price"] = target_price
    if drop_percent is not None:
        product["drop_percent"] = drop_percent
    if args.baseline_price is not None:
        product["baseline_price"] = args.baseline_price

    existing = find_product(config, url)
    if existing is not None:
        existing.clear()
        existing.update(product)
        log(f"Updated existing tracked product: {url}")
    else:
        config.setdefault("products", []).append(product)
        log(f"Added new tracked product: {url}")

    # Run an immediate check so you get instant feedback (and a baseline
    # price) instead of waiting for the next scheduled run.
    product_state = state.setdefault(url, {})
    alert = check_product(product, product_state, log)
    if alert:
        log("This item already qualifies for an alert right now.")
        try:
            send_email(resolve_email_config(config), alert["subject"], alert["body"])
            log(f"Sent email: {alert['subject']}")
        except Exception as e:
            log(f"Couldn't send the alert email ({e}). Check the 'email' section of your config.")

    return True, True  # config changed, state changed


def cmd_remove(args, config, state, log):
    url = args.url or input("Product URL to remove: ").strip()
    products = config.get("products", [])
    remaining = [p for p in products if p["url"] != url]
    if len(remaining) == len(products):
        log(f"No tracked product found with URL: {url}")
        return False, False

    config["products"] = remaining
    state.pop(url, None)
    log(f"Removed: {url}")
    return True, True


def cmd_list(args, config, state, log):
    products = config.get("products", [])
    if not products:
        print("No products are being tracked yet. Add one with:\n  python3 price_monitor.py add <url> --target-price 45")
        return False, False

    print(f"Tracked products ({len(products)}):\n")
    for i, product in enumerate(products, start=1):
        url = product["url"]
        product_state = state.get(url, {})
        name = product.get("name") or product_state.get("last_price") and url or url

        if "target_price" in product:
            rule = f"price at or below ${product['target_price']:.2f}"
        elif "drop_percent" in product:
            rule = f"price drops {product['drop_percent']:.0f}% below baseline"
        else:
            rule = "(no alert rule set - add target_price or drop_percent)"

        print(f"{i}. {product.get('name') or '(name unknown until next check)'}")
        print(f"   URL: {url}")
        print(f"   Alert when: {rule}")
        if product_state.get("last_price") is not None:
            print(f"   Last price: ${product_state['last_price']:.2f}  (baseline ${product_state.get('baseline_price', 0):.2f})")
            print(f"   Last checked: {product_state.get('last_checked')}")
        else:
            print("   Last price: not checked yet")
        if product_state.get("last_alert_price") is not None:
            print(f"   Alert already sent at: ${product_state['last_alert_price']:.2f} ({product_state.get('last_alert_at')})")
        if product_state.get("consecutive_failures"):
            print(f"   WARNING: {product_state['consecutive_failures']} failed checks in a row - {product_state.get('last_error')}")
        print()

    return False, False


def build_parser():
    parser = argparse.ArgumentParser(description="Track product prices and email yourself when they drop.")
    parser.add_argument("--config", default=os.environ.get("PRICE_MONITOR_CONFIG", "config.json"))
    parser.add_argument("--state", default=os.environ.get("PRICE_MONITOR_STATE", "state.json"))

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("check", help="Check all tracked products now and send any alerts (default).")

    add_parser = subparsers.add_parser("add", help="Start tracking a product URL.")
    add_parser.add_argument("url", nargs="?", help="Product page URL")
    add_parser.add_argument("--name", help="Friendly name (auto-detected from the page if omitted)")
    add_parser.add_argument("--target-price", type=float, help="Alert when price is at or below this amount")
    add_parser.add_argument("--drop-percent", type=float, help="Alert when price drops this %% below baseline")
    add_parser.add_argument("--baseline-price", type=float, help="Explicit baseline/full price, if known")

    remove_parser = subparsers.add_parser("remove", help="Stop tracking a product URL.")
    remove_parser.add_argument("url", nargs="?", help="Product page URL to remove")

    subparsers.add_parser("list", help="List tracked products and their last known status.")

    return parser


COMMANDS = {
    "check": cmd_check,
    "add": cmd_add,
    "remove": cmd_remove,
    "list": cmd_list,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    command = args.command or "check"

    def log(msg):
        print(f"[{now_iso()}] {msg}")

    config = load_json(args.config, None)
    if config is None:
        if command == "add":
            # Bootstrap a fresh config so `add` works even before config.json exists.
            config = {"email": {}, "products": []}
            log(f"No config found at {args.config} - creating a new one. "
                f"Remember to fill in the 'email' section before alerts can be sent.")
        else:
            log(f"Config file not found at {args.config}. Copy config.example.json to {args.config} and fill it in, "
                f"or run 'python3 price_monitor.py add <url> ...' to create one.")
            sys.exit(1)

    state = load_json(args.state, {})

    config_changed, state_changed = COMMANDS[command](args, config, state, log)

    if config_changed:
        save_json(args.config, config)
    if state_changed:
        save_json(args.state, state)


if __name__ == "__main__":
    main()
