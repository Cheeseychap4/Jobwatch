#!/usr/bin/env python3
"""
jobwatch - polls NHS Jobs search pages and pushes new adverts to Telegram.
Standard library only. No pip installs needed.

Parses each search-result card (title, employer, location, distance, salary,
closing date) rather than just the link, so adverts can be filtered on
distance and alerts carry useful detail.
"""

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
ADVERT_BASE = "https://www.jobs.nhs.uk/candidate/jobadvert/"


# Agenda for Change annual pay ranges, England, effective 1 April 2026.
# Source: NHS Employers "Pay scales for 2026/27".
AFC_BANDS = {
    "2": (25272, 25272),
    "3": (25760, 27476),
    "4": (28392, 31157),
    "5": (32073, 39043),
    "6": (39959, 48117),
    "7": (49387, 56515),
    "8a": (57528, 64750),
    "8b": (66582, 77368),
    "8c": (79504, 91609),
}


def detect_bands(title, salary):
    """Best-effort band detection. Returns a set of band labels, or an
    empty set when the advert gives no usable signal (hourly rate,
    'depends on experience', non-AfC employer)."""
    found = set()

    # 1. Band stated in the title, e.g. "Band 4", "Band 7/8a Psychologist".
    for m in re.finditer(r"band\s*([2-9])\s*([a-d])?", title, re.I):
        found.add(m.group(1) + (m.group(2).lower() if m.group(2) else ""))
    for m in re.finditer(r"band\s*[2-9][a-d]?\s*/\s*([2-9])\s*([a-d])?", title, re.I):
        found.add(m.group(1) + (m.group(2).lower() if m.group(2) else ""))
    if found:
        return found

    # 2. Otherwise infer from an annual salary range.
    pay = annual_salary_range(salary)
    if not pay:
        return found
    lo, hi = pay
    for label in AFC_BANDS:
        bmin, bmax = AFC_BANDS[label]
        if bmin <= lo <= bmax or lo <= bmin <= hi:
            found.add(label)
    return found


def band_number(label):
    m = re.match(r"([2-9])", label)
    return m.group(1) if m else label


def salary_window(labels):
    """Annual pay window spanned by a set of band labels."""
    rng = [AFC_BANDS[l] for l in AFC_BANDS if band_number(l) in labels]
    if not rng:
        return None
    return min(r[0] for r in rng), max(r[1] for r in rng)


def annual_salary_range(salary):
    """(lo, hi) annual figures from a salary string, or None if not annual."""
    if not salary or "hour" in salary.lower():
        return None
    amounts = [int(a.replace(",", "")) for a in re.findall(r"£\s*([\d,]{4,})", salary)]
    if not amounts:
        return None
    return min(amounts), max(amounts)


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


def _field(card, test_name):
    """Text of the element carrying data-test=<test_name>."""
    m = re.search(
        r'data-test="%s"[^>]*>(.*?)</(?:li|div|h3)>' % re.escape(test_name),
        card, re.S | re.I,
    )
    return strip_tags(m.group(1)) if m else ""


def parse_cards(page):
    """Return a list of dicts, one per search-result card on the page."""
    marks = [m.start() for m in
             re.finditer(r'class="nhsuk-list-panel search-result', page)]
    cards = []
    for i, start in enumerate(marks):
        end = marks[i + 1] if i + 1 < len(marks) else len(page)
        card = page[start:end]

        m = re.search(
            r'data-test="search-result-job-title"[^>]*>(.*?)</a>', card, re.S | re.I)
        if not m:
            m = re.search(
                r'href="(/candidate/jobadvert/[^"]+)"[^>]*>(.*?)</a>', card, re.S | re.I)
            if not m:
                continue
            title = strip_tags(m.group(2))
            href = html.unescape(m.group(1))
        else:
            title = strip_tags(m.group(1))
            h = re.search(r'href="(/candidate/jobadvert/[^"]+)"', card, re.I)
            href = html.unescape(h.group(1)) if h else ""

        ref = ""
        r = re.search(r"/candidate/jobadvert/([^?/#\s]+)", href)
        if r:
            ref = r.group(1)
        if not ref or not title:
            continue

        dist_text = _field(card, "search-result-distance")
        d = re.search(r"([\d.]+)\s*mile", dist_text, re.I)
        miles = float(d.group(1)) if d else None

        loc = _field(card, "search-result-location")

        cards.append({
            "ref": ref,
            "title": title,
            "url": ADVERT_BASE + ref,
            "employer_location": loc,
            "salary": _field(card, "search-result-salary").replace("Salary:", "").strip(),
            "miles": miles,
            "posted": _field(card, "search-result-publicationDate").replace("Date posted:", "").strip(),
            "closing": _field(card, "search-result-closingDate").replace("Closing date:", "").replace("Closing", "").strip(),
        })
        cards[-1]["bands"] = detect_bands(cards[-1]["title"], cards[-1]["salary"])
    return cards


def format_alert(monitor_name, c):
    bits = ["<b>%s</b>" % html.escape(c["title"])]
    if c["employer_location"]:
        bits.append(html.escape(c["employer_location"]))
    meta = []
    if c.get("bands"):
        meta.append("Band " + "/".join(sorted(c["bands"])))
    if c["miles"] is not None:
        meta.append("%.1f miles" % c["miles"])
    if c["salary"]:
        meta.append(c["salary"])
    if meta:
        bits.append(html.escape(" | ".join(meta)))
    if c["closing"]:
        bits.append("Closes: " + html.escape(c["closing"]))
    bits.append(c["url"])
    bits.append("<i>%s</i>" % html.escape(monitor_name))
    return "\n".join(bits)


def telegram(message):
    if not TOKEN or not CHAT_ID:
        print("!! Telegram secrets missing - printing instead:\n" + message)
        return
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    url = "https://api.telegram.org/bot%s/sendMessage" % TOKEN
    for attempt in range(3):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, data=payload), timeout=30
            ) as r:
                r.read()
            return
        except urllib.error.HTTPError as e:
            print("   telegram HTTP %s: %s" % (e.code, e.read()[:300]))
            return
        except Exception as e:
            print("   telegram retry %s: %s" % (attempt + 1, e))
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
        sys.exit("No monitors defined in %s" % CONFIG_FILE)

    state = load(STATE_FILE, {})
    # Global set of advert references already alerted on, from any monitor.
    # Stops the same job pinging once per search that surfaces it.
    alerted = set(state.get("_alerted", []))
    alerts = []
    scanned = {}

    for mon in monitors:
        name = mon["name"]
        print("== %s" % name)

        try:
            page = fetch(mon["url"])
        except Exception as e:
            print("   FETCH FAILED: %s" % e)
            scanned[name] = 0
            continue

        cards = parse_cards(page)
        scanned[name] = len(cards)
        if not cards:
            print("   WARNING: no result cards parsed. Page layout may have "
                  "changed, or the search returned nothing.")
            continue

        prev = state.get(name, {})
        seen = set(prev.get("seen", []))
        seeded = prev.get("seeded", False)

        title_filter = mon.get("title_filter", "")
        max_miles = mon.get("max_miles")
        want_bands = set(str(b) for b in mon.get("bands", []))
        allow_unknown = mon.get("allow_unknown_band", True)

        fresh = [c for c in cards if c["ref"] not in seen]
        kept, dropped_title, dropped_dist, dropped_dupe, dropped_band = [], 0, 0, 0, 0
        for c in fresh:
            if title_filter and not re.search(title_filter, c["title"], re.I):
                dropped_title += 1
                continue
            if max_miles is not None and c["miles"] is not None and c["miles"] > max_miles:
                dropped_dist += 1
                continue
            if want_bands:
                got = set(band_number(b) for b in c["bands"])
                if got and not (got & want_bands):
                    dropped_band += 1
                    continue
                if not got:
                    # No band matched. If the advert still quotes an annual
                    # salary, judge it against the pay window for the wanted
                    # bands (covers non-AfC employers and off-scale pay).
                    pay = annual_salary_range(c["salary"])
                    win = salary_window(want_bands)
                    if pay and win:
                        lo, hi = pay
                        wlo, whi = win[0] * 0.9, win[1] * 1.1
                        if hi < wlo or lo > whi:
                            dropped_band += 1
                            continue
                    elif not allow_unknown:
                        dropped_band += 1
                        continue
            if c["ref"] in alerted:
                dropped_dupe += 1
                continue
            kept.append(c)

        if dropped_title:
            print("   %s new but filtered out by title_filter" % dropped_title)
        if dropped_dist:
            print("   %s new but beyond %s miles" % (dropped_dist, max_miles))
        if dropped_band:
            print("   %s new but outside band %s" % (dropped_band, "/".join(sorted(want_bands))))
        if dropped_dupe:
            print("   %s already alerted under another search" % dropped_dupe)

        if not seeded:
            print("   seeding baseline with %s item(s) - no alerts sent" % len(cards))
            alerted.update(c["ref"] for c in cards)
        elif kept:
            print("   %s NEW" % len(kept))
            for c in kept[:MAX_ALERTS_PER_RUN]:
                print("      %s (%s mi, band %s)"
                      % (c["title"], c["miles"], "/".join(sorted(c["bands"])) or "?"))
                alerts.append(format_alert(name, c))
                alerted.add(c["ref"])
            if len(kept) > MAX_ALERTS_PER_RUN:
                alerts.append(
                    "<b>%s</b>\n...and %s more. Open the search page."
                    % (html.escape(name), len(kept) - MAX_ALERTS_PER_RUN))
        else:
            print("   no change")

        current = {c["ref"] for c in cards}
        merged = [c["ref"] for c in cards] + \
                 [r for r in prev.get("seen", []) if r not in current]
        state[name] = {"seen": merged[:400], "seeded": True}

    if os.environ.get("JOBWATCH_HEARTBEAT"):
        lines = ["<b>Stayin' alive</b>", "jobwatch is running."]
        for mon in monitors:
            lines.append("%s: %s adverts scanned"
                         % (mon["name"], scanned.get(mon["name"], 0)))
        broken = [n for n in scanned if scanned[n] == 0]
        if broken:
            lines.append("")
            lines.append("PROBLEM: %s returned nothing. The NHS Jobs page "
                         "layout may have changed." % ", ".join(broken))
        alerts.append("\n".join(lines))

    for msg in alerts:
        telegram(msg)

    state["_alerted"] = sorted(alerted)[:2000]

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)

    print("\nDone. %s alert(s) sent." % len(alerts))


if __name__ == "__main__":
    main()
