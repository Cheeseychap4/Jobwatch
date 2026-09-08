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


def fetch_trac_employer_cards(ids, pages=6):
    """Every live vacancy for each TRAC employer id, following pagination.

    A TRAC employer page shows at most 50 vacancies and pages the rest, so
    reading page 1 only would quietly hide everything a large trust advertises
    beyond its fiftieth vacancy - Midlands Partnership and Oxford Health both
    run past that today. Paging is session-bound, so each employer gets a
    fresh cookie jar and is walked in order until nothing new comes back."""
    out, seen = [], set()
    for eid in ids:
        _COOKIES.clear()
        base = ("https://www.healthjobsuk.com/job_list?JobSearch_re=&_ts=1"
                "&employerid=%s" % eid)
        for p in range(1, max(1, pages) + 1):
            url = base if p == 1 else base + "&_pg=%d&_pgid=" % p
            try:
                cards = parse_trac_cards(fetch(url, session=True))
            except Exception as e:
                print("   trac employer %s page %s failed: %s" % (eid, p, e))
                break
            if not cards:
                break
            fresh = [c for c in cards if c["ref"] not in seen]
            seen.update(c["ref"] for c in fresh)
            out.extend(fresh)
            if len(cards) < 50:
                break
    return out


def slug_title(url):
    """Human-ish title from the last path segment of a job URL."""
    seg = [s for s in url.split("?")[0].split("/") if s]
    if not seg:
        return ""
    s = seg[-1]
    s = re.sub(r"\.(html?|aspx)$", "", s, flags=re.I)
    s = re.sub(r"[-_]+", " ", s)
    s = re.sub(r"\s+\d+$", "", s)          # trailing id, e.g. "support worker 399"
    return s.strip().title()


def feed_probe(url, location_pattern, closed_pattern=""):
    """Open one provider advert; return (location, closed).

    Provider sitemaps carry no location, and they also lag behind closures -
    Practice Plus Group's sitemap still listed an Assistant Psychologist whose
    advert already read "Vacancy Not Found". So the same fetch does both jobs.
    Only ever called for an advert that is BOTH new and past the title filter,
    so it costs a handful of requests a run rather than hundreds."""
    try:
        page = fetch(url)
    except Exception as e:
        print("   advert lookup failed for %s: %s" % (url, e))
        return "", False
    if closed_pattern and re.search(closed_pattern, page, re.I):
        return "", True
    if not location_pattern:
        return "", False
    m = re.search(location_pattern, page, re.S | re.I)
    return (strip_tags(m.group(1)).strip() if m else ""), False


def fetch_feed_cards(mon):
    """Vacancies straight from a private provider's own careers system.

    Private hospitals do not use TRAC and several do not post everything to
    NHS Jobs, so each one is read from its own feed - the same principle as
    TRAC for the NHS: go to the system the employer actually recruits on."""
    page = fetch(mon["url"])
    employer = mon.get("employer", "")
    cards = []
    if mon.get("feed_kind", "sitemap") == "sitemap":
        for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", page):
            if mon.get("url_filter") and not re.search(mon["url_filter"], u, re.I):
                continue
            cards.append({"url": html.unescape(u), "title": slug_title(u),
                          "town": "", "salary": "", "grade": ""})
    else:
        for m in re.finditer(mon["card_pattern"], page, re.S | re.I):
            d = m.groupdict()
            u = html.unescape(d.get("url", "") or "")
            if u and u.startswith("/"):
                u = mon.get("base", "").rstrip("/") + u
            cards.append({"url": u,
                          "title": strip_tags(d.get("title", "") or ""),
                          "town": strip_tags(d.get("location", "") or ""),
                          "salary": strip_tags(d.get("salary", "") or ""),
                          "grade": ""})
    out, seen = [], set()
    for c in cards:
        if not c["title"] or not c["url"] or c["url"] in seen:
            continue
        seen.add(c["url"])
        c.update({"source": "feed", "ref": "feed:" + c["url"],
                  "employer": employer, "employer_id": "", "county": "",
                  "employer_location": ", ".join(x for x in (employer, c["town"]) if x),
                  "miles": None, "posted": "", "closing": ""})
        c["bands"] = detect_bands(c["title"], c["salary"], c["grade"])
        out.append(c)
    return out


def trac_geo(card, towns, counties, employers, exclude_towns, exclude_counties=()):
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
        # Only an employer allow-list can justify keeping a locationless card.
        # Without one, "no town" is not evidence of anything - say unknown and
        # let the monitor's unknown_location setting decide.
        emp = norm(card.get("employer"))
        if employers and any(e in emp for e in employers):
            return "in"
        return "unknown"
    # Note: the county in a TRAC advert URL is the EMPLOYER's home county,
    # not the advert's location - every Midlands Partnership advert reads
    # "Staffordshire" whatever town it is in. It is therefore useless as a
    # geography filter and exclude_counties is left empty. Towns do the work,
    # and anything unrecognised is kept and flagged rather than dropped.
    county = norm(card.get("county"))
    if exclude_counties and any(c and re.search(r"\b%s\b" % re.escape(c), county)
                                for c in exclude_counties):
        return "out"
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
    if c.get("source") == "feed":
        bits.append("Provider's own site - may not be on NHS Jobs at all")
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


def sweep(monitors, state):
    """One pass over every monitor. Returns the alerts to send."""
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
            if source == "feed":
                cards = fetch_feed_cards(mon)
            elif source == "trac_employers":
                cards = fetch_trac_employer_cards(mon.get("employer_ids", []),
                                                  mon.get("pages", 6))
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
        exclude_counties = [norm(t) for t in mon.get("exclude_counties", [])]

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
            if source == "feed" and seeded and not c["town"] \
                    and (mon.get("location_pattern") or mon.get("closed_pattern")):
                c["town"], closed = feed_probe(c["url"],
                                               mon.get("location_pattern", ""),
                                               mon.get("closed_pattern", ""))
                if closed:
                    dropped_dist += 1
                    continue
            if source.startswith("trac") or source == "feed":
                geo = trac_geo(c, towns, counties, employers, exclude_towns,
                               exclude_counties)
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

    state["_alerted"] = sorted(alerted)[:4000]
    state["_alerted_keys"] = sorted(alerted_keys)[:4000]
    return alerts


def main():
    """One pass by default.

    Set JOBWATCH_LOOP_SECONDS to keep sweeping inside a single run, checking
    every JOBWATCH_INTERVAL_SECONDS. GitHub throttles scheduled workflows on a
    busy repo - a 15-minute cron really fires every 45 to 90 minutes - so the
    way to actually check often is to stay alive between firings rather than
    ask for more firings. State is written after every pass, so a run that is
    cut short still keeps what it has seen."""
    monitors = load(CONFIG_FILE, [])
    if not monitors:
        sys.exit("No monitors defined in %s" % CONFIG_FILE)

    try:
        budget = int(os.environ.get("JOBWATCH_LOOP_SECONDS", "0"))
    except ValueError:
        budget = 0
    try:
        interval = max(60, int(os.environ.get("JOBWATCH_INTERVAL_SECONDS", "300")))
    except ValueError:
        interval = 300

    started = time.time()
    total = 0
    passes = 0
    while True:
        passes += 1
        if budget:
            print("\n----- pass %s (%.0fs into a %ss run)"
                  % (passes, time.time() - started, budget))
        state = load(STATE_FILE, {})
        alerts = sweep(monitors, state)
        for msg in alerts:
            telegram(msg)
        total += len(alerts)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1, sort_keys=True)
        os.environ.pop("JOBWATCH_HEARTBEAT", None)   # heartbeat is once per run
        elapsed = time.time() - started
        if not budget or elapsed + interval >= budget:
            break
        time.sleep(interval)

    print("\nDone. %s pass(es), %s alert(s) sent." % (passes, total))


if __name__ == "__main__":
    main()
