#!/usr/bin/env python3
"""
jobwatch - polls saved job-search URLs and pushes new adverts to Telegram.
Standard library only. No pip installs needed.
"""

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_FILE = "monitors.json"
STATE_FILE = "state.json"

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_ALERTS_PER_RUN = 12


def fetch(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-GB,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")


def strip_tags(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment)
    return html.unescape(" ".join(text.split()))


def extract_links(page, pattern, base_url):
    """Return {absolute_url: link_text} for anchors whose href matches pattern."""
    found = {}
    for m in re.finditer(
        r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*?>(.*?)</a>', page, re.S | re.I
    ):
        href, inner = m.group(1), m.group(2)
        if not re.search(pattern, href, re.I):
            continue
        text = strip_tags(inner)
        if not text or len(text) < 3:
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        absolute = absolute.split("#")[0]
        found.setdefault(absolute, text)
    return found


def telegram(message):
    if not TOKEN or not CHAT_ID:
        print("!! Telegram secrets missing - printing instead:\n" + message)
        return
    payload = urllib.parse.urlencode(
        {
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, data=payload), timeout=30
            ) as r:
                r.read()
            return
        except urllib.error.HTTPError as e:
            print(f"   telegram HTTP {e.code}: {e.read()[:300]}")
            return
        except Exception as e:
            print(f"   telegram retry {attempt + 1}: {e}")
            time.sleep(3)


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def main():
    monitors = load(CONFIG_FILE, [])
    if not monitors:
        sys.exit(f"No monitors defined in {CONFIG_FILE}")

    state = load(STATE_FILE, {})
    alerts = []

    for mon in monitors:
        name = mon["name"]
        url = mon["url"]
        pattern = mon.get("link_pattern", "")
        print(f"== {name}")

        try:
            page = fetch(url)
        except Exception as e:
            print(f"   FETCH FAILED: {e}")
            continue

        if pattern:
            items = extract_links(page, pattern, url)
            mode = "links"
        else:
            items = {}
            mode = "hash"

        if pattern and not items:
            print(f"   WARNING: pattern '{pattern}' matched nothing. "
                  f"Page may be JavaScript-rendered, or the pattern needs fixing.")
            mode = "hash"

        if mode == "hash":
            body = strip_tags(page)
            body = re.sub(r"\d{1,2}:\d{2}(:\d{2})?", "", body)
            digest = hashlib.md5(body.encode("utf-8")).hexdigest()
            items = {digest: "page content changed"}

        prev = state.get(name, {})
        seen = set(prev.get("seen", []))
        seeded = prev.get("seeded", False)

        title_filter = mon.get("title_filter", "")
        all_new = [k for k in items if k not in seen]
        if title_filter:
            new_keys = [k for k in all_new if re.search(title_filter, items[k], re.I)]
            skipped = len(all_new) - len(new_keys)
            if skipped:
                print(f"   {skipped} new but filtered out by title_filter")
        else:
            new_keys = all_new

        if not seeded:
            print(f"   seeding baseline with {len(items)} item(s) - no alerts sent")
        elif new_keys:
            print(f"   {len(new_keys)} NEW")
            for k in new_keys[:MAX_ALERTS_PER_RUN]:
                if mode == "links":
                    alerts.append(
                        f"<b>{html.escape(name)}</b>\n"
                        f"{html.escape(items[k])}\n{html.escape(k)}"
                    )
                else:
                    alerts.append(
                        f"<b>{html.escape(name)}</b>\n"
                        f"Page content changed - open and check.\n{html.escape(url)}"
                    )
            if len(new_keys) > MAX_ALERTS_PER_RUN:
                alerts.append(
                    f"<b>{html.escape(name)}</b>\n"
                    f"...and {len(new_keys) - MAX_ALERTS_PER_RUN} more. Open the search page."
                )
        else:
            print("   no change")

        # For hash mode keep only the newest digest; for links keep a rolling window.
        if mode == "hash":
            state[name] = {"seen": list(items.keys()), "seeded": True}
        else:
            merged = list(items.keys()) + [k for k in prev.get("seen", []) if k not in items]
            state[name] = {"seen": merged[:400], "seeded": True}

    for msg in alerts:
        telegram(msg)

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)

    print(f"\nDone. {len(alerts)} alert(s) sent.")


if __name__ == "__main__":
    main()
