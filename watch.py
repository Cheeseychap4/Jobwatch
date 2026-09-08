#!/usr/bin/env python3
"""
jobwatch - polls NHS Jobs AND TRAC (healthjobsuk.com) and pushes newly
posted adverts to Telegram. Standard library only. No pip installs needed.

WHY TWO SOURCES
    Most NHS trusts recruit through TRAC. The advert opens on TRAC first and
    is copied across to jobs.nhs.uk afterwards - sometimes hours later,
    sometimes days, and sometimes not until after the advert has already
    closed on an application cap. Watching jobs.nhs.uk alone therefore misses
    exactly the fast-filling posts this monitor exists to catch.
    TRAC is the source. NHS Jobs is the mirror. Watch the source.

Each monitor declares "source": "nhs" (default) or "trac".
"""

import html
import http.cookiejar
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
TRAC_BASE = "https://www.healthjobsuk.com"


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


def detect_bands(title, salary, grade=""):
    """Best-effort band detection. Returns a set of band labels, or an
    empty set when the advert gives no usable signal.

    `grade` is TRAC's own band field ("NHS AfC: Band 5"). When present it is
    authoritative - NHS Jobs search cards carry no band field at all, which
    is why the NHS-side detection has to guess from salary."""
    found = set()

    for source in (grade, title):
        if not source:
            continue
        for m in re.finditer(r"band\s*([2-9])([a-d])?\b", source, re.I):
            found.add(m.group(1) + (m.group(2).lower() if m.group(2) else ""))
        for m in re.finditer(r"band\s*[2-9][a-d]?\s*/\s*([2-9])([a-d])?\b",
                             source, re.I):
            found.add(m.group(1) + (m.group(2).lower() if m.group(2) else ""))
        if found:
            return found

    # Otherwise infer from an annual salary range.
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


# TRAC paginates against a server-side session, so paging needs cookies.
_COOKIES = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_COOKIES))


def fetch(url, session=False):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-GB,en;q=0.9",
        },
    )
    opener = _OPENER.open if session else urllib.request.urlopen
    with opener(req, timeout=60) as resp:
        raw = resp.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")


def strip_tags(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment)
    return html.unescape(" ".join(text.split()))


def norm(text):
    """Loose key for matching the same advert across the two sources."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def job_key(card):
    """Cross-source identity: employer + title, band and punctuation ignored.
    Stops a TRAC alert repeating when the job later reaches NHS Jobs.

    Employer strings differ between the two sources ("Oxford Health NHS
    Foundation Trust" vs "Oxford Health NHS Trust Bicester OX25 1PZ"), so
    only the first two words of the employer name are used."""
    title = re.sub(r"\bband\s*[2-9][a-d]?\b", " ", card["title"], flags=re.I)
    emp = card.get("employer") or card.get("employer_location", "")
    return norm(title) + "|" + " ".join(norm(emp).split()[:2])


# --------------------------------------------------------------------------
# NHS Jobs
# --------------------------------------------------------------------------

def _field(card, test_name):
    """Text of the element carrying data-test=<test_name>."""
    m = re.search(
        r'data-test="%s"[^>]*>(.*?)</(?:li|div|h3)>' % re.escape(test_name),
        card, re.S | re.I,
    )
    return strip_tags(m.group(1)) if m else ""


def parse_cards(page):
    """Return a list of dicts, one per NHS Jobs search-result card."""
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
            "source": "nhs",
            "ref": ref,
            "title": title,
            "url": ADVERT_BASE + ref,
            "employer_location": loc,
            "employer": loc,
            "town": "",
            "salary": _field(card, "search-result-salary").replace("Salary:", "").strip(),
            "miles": miles,
            "posted": _field(card, "search-result-publicationDate").replace("Date posted:", "").strip(),
            "closing": _field(card, "search-result-closingDate").replace("Closing date:", "").replace("Closing", "").strip(),
        })
        cards[-1]["bands"] = detect_bands(cards[-1]["title"], cards[-1]["salary"])
    return cards


def fetch_cards(url, pages):
    """Cards across the first `pages` NHS Jobs result pages, de-duplicated."""
    seen_refs, out = set(), []
    for p in range(1, max(1, pages) + 1):
        u = url if p == 1 else url + "&page=%d" % p
        cards = parse_cards(fetch(u))
        fresh = [c for c in cards if c["ref"] not in seen_refs]
        if not fresh:
            break
        seen_refs.update(c["ref"] for c in fresh)
        out.extend(fresh)
    return out


# --------------------------------------------------------------------------
# TRAC / healthjobsuk
# --------------------------------------------------------------------------

def _trac_field(card, cls):
    m = re.search(r'class="hj-%s hj-job-detail"[^>]*>(.*?)</div>' % cls, card, re.S)
    return strip_tags(m.group(1)) if m else ""


def parse_trac_cards(page):
    """Return a list of dicts, one per TRAC job-list card.

    TRAC publishes an explicit band ('NHS AfC: Band 5') and the county in the
    advert path, so band and geography are read rather than inferred."""
    cards = []
    for c in re.split(r'<li class="hj-job ', page)[1:]:
        h = re.search(r'href="([^"]+)"', c)
        if not h:
            continue
        href = html.unescape(h.group(1))
        title = _trac_field(c, "jobtitle")
        if not title:
            continue
        v = re.search(r"-(v\d+)", href)
        ref = "trac:" + (v.group(1) if v else norm(title)[:40].replace(" ", "-"))

        county = ""
        parts = [p for p in href.split("/") if p]
        if len(parts) > 2 and parts[0] == "job" and parts[1] == "UK":
            county = parts[2].replace("_", " ")

        salary = _trac_field(c, "salary").replace("Salary:", "").strip()
        grade = _trac_field(c, "grade")
        eid = re.search(r"employer-logos/(\d+)\.png", c)
        card = {
            "source": "trac",
            "ref": ref,
            "title": title,
            "url": TRAC_BASE + href,
            "employer": _trac_field(c, "employername"),
            "employer_id": eid.group(1) if eid else "",
            "town": _trac_field(c, "locationtown"),
            "county": county,
            "employer_location": ", ".join(
                x for x in (_trac_field(c, "employername"),
                            _trac_field(c, "locationtown")) if x),
            "salary": salary,
            "grade": grade,
            "miles": None,
            "posted": "",
            "closing": "",
        }
        card["bands"] = detect_bands(title, salary, grade)
        cards.append(card)
    return cards


def fetch_trac_cards(url, pages):
    """Newest-first TRAC cards across `pages` pages. Paging is session-based,
    so the first request must establish the sort before paging."""
    out, seen = [], set()
    page1 = fetch(url, session=True)
    for c in parse_trac_cards(page1):
        if c["ref"] not in seen:
            seen.add(c["ref"])
            out.append(c)
    base = url.split("?")[0]
    qs = urllib.parse.parse_qs(url.split("?", 1)[1]) if "?" in url else {}
    keep = {k: v[0] for k, v in qs.items() if k in ("JobSearch_re", "_ts")}
    for p in range(2, max(1, pages) + 1):
        q = dict(keep)
        q["_pg"] = str(p)
        q["_pgid"] = ""
        try:
            cards = parse_trac_cards(
                fetch(base + "?" + urllib.parse.urlencode(q), session=True))
        except Exception as e:
            print("   trac page %s failed: %s" % (p, e))
            break
        fresh = [c for c in cards if c["ref"] not in seen]
        if not fresh:
            break
        seen.update(c["ref"] for c in fresh)
        out.extend(fresh)
    return out


def fetch_trac_employer_cards(ids, pages=2):
    """Every live vacancy for each TRAC employer id.

    One short request per employer, complete for that employer, and free of
    the national list's unreliable ordering and session-bound paging."""
    out, seen = [], set()
    for eid in ids:
        url = ("https://www.healthjobsuk.com/job_list?JobSearch_re=&_ts=1"
               "&employerid=%s" % eid)
        try:
            cards = parse_trac_cards(fetch(url))
        except Exception as e:
            print("   trac employer %s failed: %s" % (eid, e))
            continue
        for c in cards:
            if c["ref"] not in seen:
                seen.add(c["ref"])
                out.append(c)
    return out


def trac_geo(card, towns, counties, employers, exclude_towns):
    """TRAC carries a town, not a distance, so geography is judged by name.

    Deliberately fails OPEN. A recognised in-radius town is kept, a recognised
    out-of-area town is dropped, and anything unrecognised is KEPT and flagged.
    An advert must never be lost because nobody had heard of the town yet —
    that is the failure mode this whole monitor exists to prevent.

    Whether "unknown" is kept or dropped is the monitor's call: keep it on the
    employer poller, where every employer is already in radius, and drop it on
    the national discovery list, where an unrecognised town is almost certainly
    the other end of the country.

    Returns "in", "out" or "unknown"."""
    town = norm(card.get("town"))
    generic = ("", "trustwide", "trust wide", "various", "various sites",
               "cross site", "multiple", "multiple sites", "countywide",
               "county wide", "agile", "hybrid", "home based")

    def hit(names):
        # Whole-word match, so "northampton" does not match "corby
        # northamptonshire" and "warwick" does not match "warwickshire".
        return any(t and re.search(r"\b%s\b" % re.escape(t), town)
                   for t in names)

    if towns and hit(towns):
        return "in"
    if counties and norm(card.get("county")) in counties:
        return "in"
    if town in generic:
        emp = norm(card.get("employer"))
        if not employers or any(e in emp for e in employers):
            return "in"
        return "unknown"
    if exclude_towns and hit(exclude_towns):
        return "out"
    return "unknown"


# --------------------------------------------------------------------------

def format_alert(monitor_name, c):
    bits = ["<b>%s</b>" % html.escape(c["title"])]
    if c["employer_location"]:
        bits.append(html.escape(c["employer_location"]))
    if c.get("bands"):
        bits.append("Band " + "/".join(sorted(c["bands"])))
    if c.get("source", "").startswith("trac"):
        bits.append("TRAC - may not be on NHS Jobs yet")
    if c.get("note"):
        bits.append(c["note"])
    bits.append(c["url"])
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
    # Advert references already alerted on, from any monitor.
    alerted = set(state.get("_alerted", []))
    # Employer+title keys already alerted on, so a job seen first on TRAC does
    # not ping again days later when the NHS Jobs mirror appears.
    alerted_keys = set(state.get("_alerted_keys", []))
    alerts = []
    scanned = {}

    for mon in monitors:
        name = mon["name"]
        source = mon.get("source", "nhs")
        print("== %s [%s]" % (name, source))

        try:
            if source == "trac_employers":
                cards = fetch_trac_employer_cards(mon.get("employer_ids", []))
            elif source == "trac":
                cards = fetch_trac_cards(mon["url"], mon.get("pages", 4))
            else:
                cards = fetch_cards(mon["url"], mon.get("pages", 3))
        except Exception as e:
            print("   FETCH FAILED: %s" % e)
            scanned[name] = 0
            continue

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
        exclude_title = mon.get("exclude_title", "")
        want_bands = set(str(b) for b in mon.get("bands", []))
        allow_unknown = mon.get("allow_unknown_band", True)
        towns = [norm(t) for t in mon.get("towns", [])]
        counties = set(norm(t) for t in mon.get("counties", []))
        employers = [norm(e) for e in mon.get("employers", [])]
        exclude_towns = [norm(t) for t in mon.get("exclude_towns", [])]

        fresh = [c for c in cards if c["ref"] not in seen]
        kept, dropped_title, dropped_dist, dropped_dupe, dropped_band = [], 0, 0, 0, 0
        dropped_senior = 0
        for c in fresh:
            if title_filter and not re.search(title_filter, c["title"], re.I):
                dropped_title += 1
                continue
            if exclude_title and re.search(exclude_title, c["title"], re.I):
                dropped_senior += 1
                continue
            if source.startswith("trac"):
                geo = trac_geo(c, towns, counties, employers, exclude_towns)
                if geo == "out":
                    dropped_dist += 1
                    continue
                if geo == "unknown":
                    if mon.get("unknown_location", "keep") == "drop":
                        dropped_dist += 1
                        continue
                    c["note"] = ("Location not in the known list (%s) - check the "
                                 "distance yourself" % (c.get("town") or "not stated"))
            elif max_miles is not None and c["miles"] is not None and c["miles"] > max_miles:
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
            if c["ref"] in alerted or job_key(c) in alerted_keys:
                dropped_dupe += 1
                continue
            known = set(str(i) for i in mon.get("known_employer_ids", []))
            if known and c.get("employer_id") and c["employer_id"] not in known:
                extra = ("New TRAC employer %s (id %s) - add the id to the "
                         "employer poller" % (c.get("employer", "?"),
                                              c["employer_id"]))
                c["note"] = (c["note"] + "\n" + extra) if c.get("note") else extra
            kept.append(c)

        if dropped_title:
            print("   %s new but filtered out by title_filter" % dropped_title)
        if dropped_senior:
            print("   %s new but too senior" % dropped_senior)
        if dropped_dist:
            print("   %s new but out of area" % dropped_dist)
        if dropped_band:
            print("   %s new but outside band %s" % (dropped_band, "/".join(sorted(want_bands))))
        if dropped_dupe:
            print("   %s already alerted under another search or source" % dropped_dupe)

        if not seeded:
            print("   seeding baseline with %s item(s) - no alerts sent" % len(cards))
            alerted.update(c["ref"] for c in cards)
            alerted_keys.update(job_key(c) for c in cards)
        elif kept:
            print("   %s NEW" % len(kept))
            for c in kept[:MAX_ALERTS_PER_RUN]:
                print("      %s (%s, band %s)"
                      % (c["title"], c.get("town") or c["miles"],
                         "/".join(sorted(c["bands"])) or "?"))
                alerts.append(format_alert(name, c))
                alerted.add(c["ref"])
                alerted_keys.add(job_key(c))
            if len(kept) > MAX_ALERTS_PER_RUN:
                alerts.append(
                    "<b>%s</b>\n...and %s more. Open the search page."
                    % (html.escape(name), len(kept) - MAX_ALERTS_PER_RUN))
        else:
            print("   no change")

        current = {c["ref"] for c in cards}
        merged = [c["ref"] for c in cards] + \
                 [r for r in prev.get("seen", []) if r not in current]
        state[name] = {"seen": merged[:600], "seeded": True}

    if os.environ.get("JOBWATCH_HEARTBEAT"):
        lines = ["<b>Stayin' alive</b>", "jobwatch is running."]
        for mon in monitors:
            lines.append("%s: %s adverts scanned"
                         % (mon["name"], scanned.get(mon["name"], 0)))
        broken = [n for n in scanned if scanned[n] == 0]
        if broken:
            lines.append("")
            lines.append("PROBLEM: %s returned nothing. The page layout may "
                         "have changed." % ", ".join(broken))
        alerts.append("\n".join(lines))

    for msg in alerts:
        telegram(msg)

    state["_alerted"] = sorted(alerted)[:4000]
    state["_alerted_keys"] = sorted(alerted_keys)[:4000]

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)

    print("\nDone. %s alert(s) sent." % len(alerts))


if __name__ == "__main__":
    main()
