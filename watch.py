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

MAX_ALERTS_PER_MONITOR = 12
# How long a monitor is allowed to keep scanning nothing before it says so
# again. Long enough that an empty search is not a daily nag, short enough
# that a dead monitor cannot hide until the weekly heartbeat.
ZERO_ALERT_SECONDS = 24 * 3600
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
    """(lo, hi) annual figures from a salary string, or None if not annual.

    Council adverts are messier than NHS ones. Some quote no currency symbol
    at all ("27052 - 29345"), some bury the figures in brackets after a grade
    ("Band C SCP 5-8 (£25,583- £26,824 per annum)"), and some quote an hourly
    rate beside an annual one. So: prefer figures marked with a pound sign,
    fall back to bare numbers, and treat anything under £5,000 as a pro-rata
    actual rather than a full-time salary."""
    if not salary:
        return None
    text = salary.replace(",", "")
    amounts = [int(a.split(".")[0])
               for a in re.findall(r"£\s*(\d{4,}(?:\.\d+)?)", text)]
    if not amounts:
        # No currency symbol. Bare four-to-six digit numbers only, so a grade
        # ("SCP 5-8") or a year ("2026") is not mistaken for pay.
        amounts = [int(a) for a in re.findall(r"(?<![\d.£])(\d{5,6})(?![\d.])", text)]
    amounts = [a for a in amounts if 5000 <= a <= 300000]
    if not amounts:
        return None
    if "hour" in salary.lower() and max(amounts) < 10000:
        return None
    return min(amounts), max(amounts)


# TRAC paginates against a server-side session, so paging needs cookies.
_COOKIES = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_COOKIES))


# One pass often asks for the same page twice - the psychology poller and the
# support poller read the same 31 TRAC employers - so pages are cached for the
# length of a pass and the cache is cleared between passes. Same coverage, half
# the requests, and kinder to the job boards.
_CACHE = {}

# Fetch errors, keyed by monitor name. A source that refuses the runner and a
# search that genuinely has no hits both end a pass with nothing; only this
# register tells them apart, so the weekly heartbeat can say which happened
# instead of blaming the page layout for both.
_FETCH_ERRORS = {}


def note_fetch_error(monitor, detail):
    _FETCH_ERRORS.setdefault(monitor, []).append(detail)
_STATS = {"fetched": 0, "cached": 0, "retried": 0}


def fetch(url, session=False):
    if url in _CACHE:
        _STATS["cached"] += 1
        return _CACHE[url]
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-GB,en;q=0.9",
        },
    )
    opener = _OPENER.open if session else urllib.request.urlopen
    # A dropped connection must never quietly cost a whole employer or keyword,
    # so a transient failure is retried before it is allowed to become an error.
    for attempt in range(3):
        try:
            with opener(req, timeout=60) as resp:
                raw = resp.read()
            break
        except urllib.error.HTTPError as e:
            # 404 and 403 are answers, not accidents - retrying them only
            # wastes the pass. Rate limits and gateway errors are the ones
            # that clear on their own, so those are the ones worth waiting on.
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                _STATS["retried"] += 1
                print("   retry %s for %s (%s)" % (attempt + 1,
                                                   url.split("?")[0], e))
                time.sleep(5 + 10 * attempt)
                continue
            raise
        except Exception as e:
            if attempt == 2:
                raise
            _STATS["retried"] += 1
            print("   retry %s for %s (%s)" % (attempt + 1, url.split("?")[0], e))
            time.sleep(2 + 3 * attempt)
    try:
        page = raw.decode("utf-8")
    except UnicodeDecodeError:
        page = raw.decode("latin-1", "replace")
    _STATS["fetched"] += 1
    _CACHE[url] = page
    return page


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
    kind = mon.get("feed_kind", "sitemap")
    if kind == "sitemap":
        for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", page):
            if mon.get("url_filter") and not re.search(mon["url_filter"], u, re.I):
                continue
            cards.append({"url": html.unescape(u), "title": slug_title(u),
                          "town": "", "salary": "", "grade": ""})
    elif kind == "links":
        # An ordinary HTML page that links out to each vacancy on an applicant
        # tracking system. Used where the ATS itself renders through JavaScript
        # and cannot be read, but the charity's own site lists the adverts.
        # Title comes from the slug and the location from a body probe.
        for u in re.findall(r'href="([^"]+)"', page):
            u = html.unescape(u)
            if mon.get("url_filter") and not re.search(mon["url_filter"], u, re.I):
                continue
            if u.startswith("/"):
                u = mon.get("base", "").rstrip("/") + u
            cards.append({"url": u, "title": slug_title(u),
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


def fetch_smartrecruiters_cards(mon):
    """Every live posting for a SmartRecruiters company, from its public API.

    Several of the criminal justice charities recruit through SmartRecruiters,
    which publishes an unauthenticated JSON endpoint - title, city and posting
    id, with no HTML to parse and no layout to break."""
    out = []
    company = mon["company"]
    employer = mon.get("employer", company)
    offset, total = 0, None
    while True:
        url = ("https://api.smartrecruiters.com/v1/companies/%s/postings"
               "?limit=100&offset=%d" % (urllib.parse.quote(company), offset))
        try:
            data = json.loads(fetch(url))
        except Exception as e:
            print("   smartrecruiters '%s' failed: %s" % (company, e))
            break
        content = data.get("content", [])
        total = data.get("totalFound", len(content))
        for j in content:
            loc = j.get("location") or {}
            town = loc.get("city") or ""
            ref = j.get("id") or j.get("uuid") or ""
            out.append({
                "source": "smartrecruiters",
                "ref": "sr-" + str(ref),
                "title": j.get("name", ""),
                "url": ("https://jobs.smartrecruiters.com/%s/%s"
                        % (company, ref)),
                "employer": employer,
                "employer_location": ", ".join(x for x in (employer, town) if x),
                "town": town,
                "county": loc.get("region") or "",
                "salary": "",
                "contract": j.get("typeOfEmployment", {}).get("label", "")
                            if isinstance(j.get("typeOfEmployment"), dict) else "",
                "miles": None,
                "posted": (j.get("releasedDate") or "")[:10],
                "closing": "",
            })
            out[-1]["bands"] = detect_bands(out[-1]["title"], "")
        offset += len(content)
        if not content or offset >= total:
            break
    return out


def probe_wmjobs(card):
    """Read contract type, hours, salary and closing date off a wmjobs advert.

    The search card carries none of them, so the income-fork filters would
    otherwise have nothing to test - a permanent-only rule that never fires is
    worse than no rule, because it reads as if it did. Only ever called for a
    card that has already passed the title and employer filters, so the extra
    fetch is one per genuinely new advert, not one per advert seen."""
    try:
        page = fetch(card["url"])
    except Exception as e:
        print("   wmjobs probe failed for %s: %s" % (card["url"], e))
        return

    def grab(pattern):
        m = re.search(pattern, page, re.S | re.I)
        return strip_tags(m.group(1)).strip() if m else ""

    contract = grab(r'"Contract Type"\s*:\s*"([^"]*)"')
    hours = grab(r'"Hours"\s*:\s*"([^"]*)"')
    if contract or hours:
        card["contract"] = " ".join(x for x in (contract, hours) if x)
    closing = grab(r'Closing date</dt>\s*<dd[^>]*>(.*?)</dd>')
    if closing:
        card["closing"] = closing
    salary = grab(r'"SalaryDescription"\s*:\s*"([^"]*)"')
    if salary and not annual_salary_range(card.get("salary", "")):
        card["salary"] = salary
        card["bands"] = detect_bands(card["title"], salary)


MOJ_BASE = "https://jobs.justice.gov.uk"


def parse_moj_cards(page):
    """Cards from an MoJ / HMPPS jobs board search page.

    The board publishes a salary BAND rather than a grade and a Business Unit
    rather than a town, so grade is judged on the salary ceiling (Pool 4.3) and
    geography on the establishment name."""
    cards = []
    for a in re.split(r'<article class="article article--result', page)[1:]:
        h = re.search(r'href="(%s/careers/JobDetail/[^"]+)"' % re.escape(MOJ_BASE), a)
        if not h:
            continue
        url = html.unescape(h.group(1))
        text = strip_tags(a)
        # The title is the anchor's own text; the article tag is split open by
        # the delimiter above, so the flattened text starts with tag leftovers.
        t = re.search(r'href="%s/careers/JobDetail/[^"]+"[^>]*>(.*?)</a>'
                      % re.escape(MOJ_BASE), a, re.S)
        title = strip_tags(t.group(1)) if t else ""
        title = re.sub(r"^\d+\s*[-:]\s*", "", title).strip()
        if not title:
            continue

        def field(name, nxt):
            m = re.search(r"%s:\s*(.*?)\s*(?:%s:|$)" % (name, nxt), text)
            return m.group(1).strip() if m else ""

        salary = field("Salary", "Business Unit")
        unit = field("Business Unit", "Closing Date")
        closing = field("Closing Date", "Working Pattern")
        ref = re.search(r"/JobDetail/[^/]*/(\d+)", url)
        cards.append({
            "source": "moj",
            "ref": "moj:" + (ref.group(1) if ref else url),
            "title": title,
            "url": url,
            "employer": "HMPPS / Ministry of Justice",
            "employer_id": "",
            "town": unit,
            "county": "",
            "employer_location": "HMPPS / MoJ, " + unit if unit else "HMPPS / MoJ",
            "geo_extra": title,
            "salary": salary,
            "grade": "",
            "miles": None,
            "posted": "",
            "closing": closing,
            "bands": set(),
        })
    return cards


def fetch_moj_cards(mon):
    """One search per keyword on the MoJ board. Keywords are the house terms -
    the same job is a Group Worker at one prison and a Facilitator at the next,
    so one term is never a sweep."""
    out, seen = [], set()
    for kw in mon.get("keywords", []):
        url = "%s/careers/SearchJobs/%s/" % (MOJ_BASE, urllib.parse.quote(kw))
        try:
            cards = parse_moj_cards(fetch(url))
        except Exception as e:
            print("   moj '%s' failed: %s" % (kw, e))
            continue
        for c in cards:
            if c["ref"] not in seen:
                seen.add(c["ref"])
                out.append(c)
    return out


# --------------------------------------------------------------------------
# Local authority sources. The councils are the one seam the NHS and justice
# monitors cannot see at all, and they are where Track B actually lives -
# information rights, business support, residential care. wmjobs is the source
# for most West Midlands councils and the mirror for those running their own
# ATS; Coventry runs its own (Tribepad) and that one is the source.
# --------------------------------------------------------------------------

WMJOBS_BASE = "https://www.wmjobs.co.uk"


def parse_wmjobs_cards(page):
    """Return a list of dicts, one per wmjobs search-result card."""
    marks = [m.start() for m in re.finditer(r'class="lister__item', page)]
    cards = []
    for i, start in enumerate(marks):
        end = marks[i + 1] if i + 1 < len(marks) else len(page)
        card = page[start:end]

        ref = re.search(r'id="item-(\d+)"', card)
        title = re.search(
            r'class="lister__header"><a\s+href="\s*([^"]+?)\s*"[^>]*>'
            r'<span>(.*?)</span>', card, re.S | re.I)
        if not ref or not title:
            continue
        jid = ref.group(1)

        def meta(kind):
            m = re.search(
                r'lister__meta-item--%s">(.*?)</li>' % kind, card, re.S | re.I)
            return strip_tags(m.group(1)) if m else ""

        # wmjobs does not print a closing date on the card, only a countdown
        # ("1 day left", "Expiring today"). That is enough to make urgency
        # visible in the ping; the real date is read off the advert body.
        left = re.search(r'class="text-error">([^<]*)</span>', card, re.I)

        loc = meta("location")
        employer = meta("recruiter")
        cards.append({
            "source": "wmjobs",
            "ref": "wmjobs-" + jid,
            "title": strip_tags(title.group(2)),
            "url": "%s/job/%s/" % (WMJOBS_BASE, jid),
            "employer": employer,
            "employer_location": (employer + " - " + loc).strip(" -"),
            "town": loc,
            "county": "",
            "salary": meta("salary"),
            "contract": "",
            "miles": None,
            "posted": "",
            "closing": strip_tags(left.group(1)) if left else "",
        })
        cards[-1]["bands"] = detect_bands(cards[-1]["title"],
                                          cards[-1]["salary"])
    return cards


def parse_wmjobs_rss(xml):
    """Return a list of dicts, one per advert in a wmjobs RSS feed.

    The feed is the machine-readable face of the same search - employer,
    title, salary, town, link and posting date - and it is served to a
    plain client, where the HTML search page is answered with 403 for the
    GitHub Actions runner."""
    cards = []
    for raw in re.findall(r"<item>(.*?)</item>", xml, re.S):
        link = re.search(r"<link>(.*?)</link>", raw, re.S)
        head = re.search(r"<title>(.*?)</title>", raw, re.S)
        if not link or not head:
            continue
        url = html.unescape(link.group(1)).strip().split("?")[0]
        jid = re.search(r"/job/(\d+)", url)
        if not jid:
            continue
        # "Warwickshire County Council: Information Rights Officer"
        employer, sep, title = strip_tags(head.group(1)).partition(": ")
        if not sep:
            employer, title = "", employer

        desc = re.search(r"<description>(.*?)</description>", raw, re.S)
        body = html.unescape(re.sub(r"<[^>]+>", " ", desc.group(1))) if desc else ""
        lines = [x.strip() for x in body.split("\n") if x.strip()]
        # Salary first, employer, blurb, town last.
        salary = lines[0].rstrip(":").strip() if lines else ""
        if norm(salary) == norm(employer):
            salary = ""
        town = lines[-1] if len(lines) > 1 else ""
        # A truncated blurb ends in an ellipsis, and that is never a place.
        if town.endswith("...") or town.endswith("\u2026") \
                or norm(town) == norm(employer) or norm(town) == norm(salary):
            town = ""
        posted = re.search(r"<pubDate>(.*?)</pubDate>", raw, re.S)

        cards.append({
            "source": "wmjobs",
            "ref": "wmjobs-" + jid.group(1),
            "title": title.strip(),
            "url": url,
            "employer": employer.strip(),
            "employer_location": (employer.strip() + " - " + town).strip(" -"),
            "town": town,
            "county": "",
            "salary": salary,
            "contract": "",
            "miles": None,
            "posted": strip_tags(posted.group(1)) if posted else "",
            "closing": "",
        })
        cards[-1]["bands"] = detect_bands(cards[-1]["title"],
                                          cards[-1]["salary"])
    return cards


def fetch_wmjobs_cards(mon):
    """One search per keyword on wmjobs, de-duplicated across keywords.

    The RSS feed is read first and the HTML search page is only the
    fallback. wmjobs sits behind a bot rule that answers the GitHub
    Actions runner with 403 on the HTML page, which is why both council
    monitors scanned nothing on every run before this. Both are asked for
    newest first, so a fresh advert cannot sit below the twenty results
    that a relevance-ranked page returns."""
    out, seen = [], set()
    for kw in mon.get("keywords", []):
        q = urllib.parse.quote_plus(kw)
        cards, feed = [], None
        try:
            feed = fetch("%s/jobsrss/?keywords=%s&sort=Date" % (WMJOBS_BASE, q))
        except urllib.error.HTTPError as e:
            # wmjobs answers a keyword with no hits with a 404. That is an
            # empty search, not a broken source - say so, so a real breakage
            # still stands out in the log.
            if e.code == 404:
                print("   wmjobs '%s': no results" % kw)
                continue
            print("   wmjobs feed '%s' refused (%s), trying the search page"
                  % (kw, e))
        except Exception as e:
            print("   wmjobs feed '%s' failed (%s), trying the search page"
                  % (kw, e))

        if feed is not None:
            cards = parse_wmjobs_rss(feed)
            if not cards:
                print("   wmjobs '%s': no results" % kw)
        else:
            try:
                cards = parse_wmjobs_cards(
                    fetch("%s/jobs/?keywords=%s&sort=Date" % (WMJOBS_BASE, q)))
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    print("   wmjobs '%s': no results" % kw)
                else:
                    note_fetch_error(mon["name"], "%s: %s" % (kw, e))
                    print("   wmjobs '%s' failed: %s" % (kw, e))
                continue
            except Exception as e:
                note_fetch_error(mon["name"], "%s: %s" % (kw, e))
                print("   wmjobs '%s' failed: %s" % (kw, e))
                continue

        for c in cards:
            if c["ref"] not in seen:
                seen.add(c["ref"])
                out.append(c)
    return out


def parse_tribepad_cards(page):
    """Return a list of dicts, one per Tribepad (Coventry CC) result card.

    Richer than wmjobs: the card itself carries contract type, the closing
    date and the posting date, so nothing has to be inferred."""
    marks = [m.start() for m in re.finditer(r'class="job-list-title"', page)]
    cards = []
    for i, start in enumerate(marks):
        end = marks[i + 1] if i + 1 < len(marks) else len(page)
        # The href sits just above the title, so reach back for it.
        head = page[max(0, start - 600):start]
        card = page[start:end]

        href = re.findall(r'href="(https?://[^"]*/jobs/job/[^"]+)"', head)
        title = re.search(r'class="job-list-title">(.*?)</span>', card, re.S)
        if not href or not title:
            continue
        url = html.unescape(href[-1])
        rid = re.search(r"/(\d+)/?$", url.split("?")[0])

        def grab(pattern):
            m = re.search(pattern, card, re.S | re.I)
            return strip_tags(m.group(1)) if m else ""

        loc = grab(r"itemprop='address'>(.*?)</span>")
        # Tribepad appends a country to every address ("..., Coventry, United
        # Kingdom (Incl. Northern Ireland)"), which is noise in a two-line
        # alert and never helps the town match.
        loc = re.sub(r",?\s*United Kingdom\s*\(Incl\.? Northern Ireland\)\s*$",
                     "", loc).strip().strip(",").strip()
        cards.append({
            "source": "tribepad",
            "ref": "tribepad-" + (rid.group(1) if rid else url[-40:]),
            "title": strip_tags(title.group(1)),
            "url": url,
            "employer": "Coventry City Council",
            "employer_location": ("Coventry City Council - " + loc).strip(" -"),
            "town": loc,
            "county": "",
            "salary": grab(r"fa-wallet'></i>(.*?)</p>"),
            "contract": grab(r"itemprop='employmentType'>(.*?)</p>"),
            "miles": None,
            "posted": grab(r"datePosted'>(.*?)</span>"),
            "closing": grab(r"Apply by(.*?)</p>"),
        })
        cards[-1]["bands"] = detect_bands(cards[-1]["title"],
                                          cards[-1]["salary"])
    return cards


def fetch_tribepad_cards(mon):
    """Every page of a Tribepad careers site. Paging is stateless here -
    /jobs/search/-1/<n> - so the whole list is reachable in one pass."""
    out, seen = [], set()
    base = mon["url"].rstrip("/")
    for p in range(1, max(1, mon.get("pages", 10)) + 1):
        url = base if p == 1 else "%s/jobs/search/-1/%d" % (base, p)
        try:
            cards = parse_tribepad_cards(fetch(url))
        except Exception as e:
            print("   tribepad page %s failed: %s" % (p, e))
            break
        if not cards:
            break
        fresh = [c for c in cards if c["ref"] not in seen]
        if not fresh:
            break
        seen.update(c["ref"] for c in fresh)
        out.extend(fresh)
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
    # Some boards name a service rather than a place ("Psychology Services",
    # "NPS Wales UM Transition"), and put the establishment in the title
    # instead - so both are read.
    town = norm((card.get("town") or "") + " " + (card.get("geo_extra") or ""))
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
    """Two lines: the role, hyperlinked, and where it is.

    Everything else - band, salary, contract type, closing date, which source
    found it - is one tap away on the advert itself, and a ping is for
    deciding whether to open it. The one thing that stays is the flag for an
    unrecognised location, because that is the alert saying it could not do
    the geography for you."""
    link = '<a href="%s">%s</a>' % (html.escape(c["url"], quote=True),
                                    html.escape(c["title"]))
    where = c.get("employer_location") or c.get("town") or ""
    bits = [link]
    if where:
        bits.append(html.escape(where))
    if c.get("note"):
        # Keep it to the short form - the long explanation was written for a
        # log, not for a phone.
        bits.append("Location not recognised - check the distance"
                    if "not in the known list" in c["note"]
                    else html.escape(c["note"].split("\n")[0]))
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


def sweep(monitors, state, first_pass=True):
    """One pass over every monitor. Returns the alerts to send."""
    _CACHE.clear()
    _FETCH_ERRORS.clear()
    _STATS.update(fetched=0, cached=0, retried=0)
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
        if not first_pass and not mon.get("every_pass", True):
            continue
        print("== %s [%s]" % (name, source))

        try:
            if source == "moj":
                cards = fetch_moj_cards(mon)
            elif source == "feed":
                cards = fetch_feed_cards(mon)
            elif source == "trac_employers":
                cards = fetch_trac_employer_cards(mon.get("employer_ids", []),
                                                  mon.get("pages", 6))
            elif source == "trac":
                cards = fetch_trac_cards(mon["url"], mon.get("pages", 4))
            elif source == "wmjobs":
                cards = fetch_wmjobs_cards(mon)
            elif source == "tribepad":
                cards = fetch_tribepad_cards(mon)
            elif source == "smartrecruiters":
                cards = fetch_smartrecruiters_cards(mon)
            else:
                cards = fetch_cards(mon["url"], mon.get("pages", 3))
        except Exception as e:
            print("   FETCH FAILED: %s" % e)
            note_fetch_error(name, str(e))
            scanned[name] = 0
            continue

        scanned[name] = len(cards)
        zero = state.setdefault("_zero", {})
        if not cards:
            print("   WARNING: no result cards parsed. Page layout may have "
                  "changed, or the search returned nothing.")
            # The weekly heartbeat is too slow to be the only alarm - a
            # monitor that dies on a Tuesday would sit dead until the
            # following Monday. Say it now, then stay quiet about it for a
            # day so a genuinely empty search cannot become a daily nag.
            if time.time() - zero.get(name, 0) > ZERO_ALERT_SECONDS:
                zero[name] = time.time()
                errs = _FETCH_ERRORS.get(name)
                if errs:
                    why = ("the source refused %s of the requests (%s)"
                           % (len(errs), errs[0]))
                else:
                    why = ("either the search has no hits at all or the page "
                           "layout changed")
                alerts.append("<b>jobwatch problem</b>\n%s scanned nothing - %s."
                              % (html.escape(name), html.escape(why)))
            continue
        zero.pop(name, None)

        prev = state.get(name, {})
        seen = set(prev.get("seen", []))
        seeded = prev.get("seeded", False)

        title_filter = mon.get("title_filter", "")
        max_miles = mon.get("max_miles")
        exclude_title = mon.get("exclude_title", "")
        exclude_discipline = mon.get("exclude_discipline", "")
        protect_title = mon.get("protect_title", "psycholog")
        want_bands = set(str(b) for b in mon.get("bands", []))
        allow_unknown = mon.get("allow_unknown_band", True)
        towns = [norm(t) for t in mon.get("towns", [])]
        counties = set(norm(t) for t in mon.get("counties", []))
        employers = [norm(e) for e in mon.get("employers", [])]
        exclude_towns = [norm(t) for t in mon.get("exclude_towns", [])]
        exclude_counties = [norm(t) for t in mon.get("exclude_counties", [])]
        exclude_employers = [norm(e) for e in mon.get("exclude_employers", [])]
        contract_filter = mon.get("contract_filter", "")
        exclude_contract = mon.get("exclude_contract", "")

        fresh = [c for c in cards if c["ref"] not in seen]
        kept, dropped_title, dropped_dist, dropped_dupe, dropped_band = [], 0, 0, 0, 0
        dropped_senior = 0
        dropped_discipline = 0
        dropped_pay = 0
        dropped_contract = 0
        dropped_employer = 0
        for c in fresh:
            if exclude_employers:
                emp = norm((c.get("employer") or "") + " "
                           + (c.get("employer_location") or ""))
                if any(e and e in emp for e in exclude_employers):
                    dropped_employer += 1
                    continue
            if title_filter and not re.search(title_filter, c["title"], re.I):
                dropped_title += 1
                continue
            if exclude_title and re.search(exclude_title, c["title"], re.I):
                dropped_senior += 1
                continue
            # Other allied health professions. "Assistant" is the word that also
            # spells Assistant Psychologist, so the discipline is excluded by
            # name rather than the grade - and a title that says psychology is
            # never dropped by this, so "Assistant Psychologist (maternity
            # cover)" survives a maternity exclusion.
            if exclude_discipline and re.search(exclude_discipline, c["title"], re.I) \
                    and not (protect_title and re.search(protect_title, c["title"], re.I)):
                dropped_discipline += 1
                continue
            if source == "feed" and seeded and not c["town"] \
                    and (mon.get("location_pattern") or mon.get("closed_pattern")):
                c["town"], closed = feed_probe(c["url"],
                                               mon.get("location_pattern", ""),
                                               mon.get("closed_pattern", ""))
                if closed:
                    dropped_dist += 1
                    continue
            # wmjobs prints no contract type on the card, so a monitor that
            # filters on one has to read the advert. Done here, after the
            # cheap filters, so it costs one fetch per genuinely new advert.
            if source == "wmjobs" and seeded and mon.get("probe_body") \
                    and (contract_filter or exclude_contract
                         or mon.get("min_salary")):
                probe_wmjobs(c)
            if source.startswith("trac") or source in (
                    "feed", "moj", "wmjobs", "tribepad", "smartrecruiters"):
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
            max_salary = mon.get("max_salary")
            if max_salary:
                pay = annual_salary_range(c["salary"])
                if pay and pay[0] > max_salary:
                    dropped_band += 1
                    continue
            # Track B carries the only salary floor in the search. A band that
            # STARTS below the floor and tops out above it is kept, because the
            # pool says to state the split rather than cut it silently - so the
            # test is on the top of the advertised range.
            min_salary = mon.get("min_salary")
            if min_salary:
                pay = annual_salary_range(c["salary"])
                if pay and pay[1] < min_salary:
                    dropped_pay += 1
                    continue
            # Contract type, where the board publishes it. Track B is permanent
            # full time only. Fails OPEN: wmjobs does not print a contract type
            # on the card, and an advert must never be lost to a field that was
            # simply absent.
            contract = c.get("contract", "")
            if contract:
                if exclude_contract and re.search(exclude_contract, contract, re.I):
                    dropped_contract += 1
                    continue
                if contract_filter and not re.search(contract_filter, contract, re.I):
                    dropped_contract += 1
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
        if dropped_discipline:
            print("   %s new but another clinical discipline" % dropped_discipline)
        if dropped_dist:
            print("   %s new but out of area" % dropped_dist)
        if dropped_band:
            print("   %s new but outside band %s" % (dropped_band, "/".join(sorted(want_bands))))
        if dropped_pay:
            print("   %s new but below the salary floor" % dropped_pay)
        if dropped_contract:
            print("   %s new but the wrong contract type" % dropped_contract)
        if dropped_employer:
            print("   %s new but a blocked or out-of-region employer" % dropped_employer)
        if dropped_dupe:
            print("   %s already alerted under another search or source" % dropped_dupe)

        if not seeded:
            print("   seeding baseline with %s item(s) - no alerts sent" % len(cards))
            alerted.update(c["ref"] for c in cards)
            alerted_keys.update(job_key(c) for c in cards)
        elif kept:
            print("   %s NEW" % len(kept))
            for c in kept[:MAX_ALERTS_PER_MONITOR]:
                print("      %s (%s, band %s)"
                      % (c["title"], c.get("town") or c["miles"],
                         "/".join(sorted(c["bands"])) or "?"))
                alerts.append(format_alert(name, c))
                alerted.add(c["ref"])
                alerted_keys.add(job_key(c))
            if len(kept) > MAX_ALERTS_PER_MONITOR:
                alerts.append(
                    "<b>%s</b>\n...and %s more. Open the search page."
                    % (html.escape(name), len(kept) - MAX_ALERTS_PER_MONITOR))
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
            for n in broken:
                errs = _FETCH_ERRORS.get(n)
                if errs:
                    lines.append("PROBLEM: %s - the source refused %s of the "
                                 "requests (%s). Nothing to do with the search "
                                 "terms." % (n, len(errs), errs[0]))
                else:
                    lines.append("PROBLEM: %s returned nothing. Either the "
                                 "search genuinely has no hits or the page "
                                 "layout changed." % n)
        alerts.append("\n".join(lines))

    print("\n%s page(s) fetched, %s served from this pass's cache, %s retried"
          % (_STATS["fetched"], _STATS["cached"], _STATS["retried"]))

    state["_alerted"] = sorted(alerted)[:4000]
    state["_alerted_keys"] = sorted(alerted_keys)[:4000]
    return alerts


REQUIRED_KEYS = {
    "nhs": ["url"],
    "trac": ["url"],
    "trac_employers": ["employer_ids"],
    "feed": ["url"],
    "moj": ["keywords"],
    "wmjobs": ["keywords"],
    "tribepad": ["url"],
    "smartrecruiters": ["company"],
}
REGEX_KEYS = ("title_filter", "exclude_title", "exclude_discipline",
              "protect_title", "contract_filter", "exclude_contract",
              "location_pattern", "closed_pattern", "card_pattern")


def check_monitors(monitors):
    """Validate monitors.json without touching the network.

    Every filter in this file is a regex compiled at match time, so a stray
    bracket does not fail loudly - it raises inside one monitor's loop and
    that monitor quietly stops matching. This catches that before a run does,
    and is cheap enough to put in front of every deploy."""
    problems, names = [], set()
    for i, mon in enumerate(monitors):
        where = mon.get("name") or "monitor %d" % i
        if not mon.get("name"):
            problems.append("%s: no name (name is the state key)" % where)
        elif mon["name"] in names:
            problems.append("%s: duplicate name - the two share state" % where)
        names.add(mon.get("name"))

        source = mon.get("source", "nhs")
        if source not in REQUIRED_KEYS:
            problems.append("%s: unknown source %r" % (where, source))
        else:
            for key in REQUIRED_KEYS[source]:
                if not mon.get(key):
                    problems.append("%s: source %s needs %r"
                                    % (where, source, key))

        for key in REGEX_KEYS:
            pattern = mon.get(key)
            if not pattern:
                continue
            try:
                re.compile(pattern)
            except re.error as e:
                problems.append("%s: %s is not a valid regex (%s)"
                                % (where, key, e))

        lo, hi = mon.get("min_salary"), mon.get("max_salary")
        if lo and hi and lo > hi:
            problems.append("%s: min_salary above max_salary - matches nothing"
                            % where)
        if (mon.get("contract_filter") or mon.get("exclude_contract")) \
                and source == "wmjobs" and not mon.get("probe_body"):
            problems.append("%s: contract filter set but probe_body is not - "
                            "wmjobs cards carry no contract type, so the "
                            "filter would never fire" % where)

    for p in problems:
        print("PROBLEM  " + p)
    print("%d monitor(s) checked, %d problem(s)." % (len(monitors), len(problems)))
    return 1 if problems else 0


def scan_trac(lo=1, hi=5000):
    """Probe every TRAC employer id and report those advertising in radius.

    The employer id list is a snapshot: an employer that adopts TRAC after the
    last scan is invisible until the next one. Run this monthly and diff the
    result against the ids in monitors.json."""
    towns = set()
    for mon in load(CONFIG_FILE, []):
        if mon.get("source") == "trac_employers":
            towns.update(norm(t) for t in mon.get("towns", []))
    known = set()
    for mon in load(CONFIG_FILE, []):
        known.update(str(i) for i in mon.get("employer_ids", []))
    found = {}
    for eid in range(lo, hi + 1):
        url = ("https://www.healthjobsuk.com/job_list?JobSearch_re=&_ts=1"
               "&employerid=%d" % eid)
        try:
            cards = parse_trac_cards(fetch(url))
        except Exception:
            continue
        if not cards:
            continue
        for c in cards:
            town = norm(c.get("town", ""))
            if any(t and re.search(r"\b%s\b" % re.escape(t), town) for t in towns):
                found[str(eid)] = c.get("employer", "?")
                break
        if eid % 250 == 0:
            print("   ...scanned to id %d, %d in radius so far" % (eid, len(found)))
    print("\n%d employer(s) advertising in radius:" % len(found))
    for eid, emp in sorted(found.items(), key=lambda kv: int(kv[0])):
        print("   %s%s  %s" % (eid, "" if eid in known else "  NEW", emp))
    missing = [e for e in found if e not in known]
    print("\n%d new id(s) to add to monitors.json: %s"
          % (len(missing), ", ".join(sorted(missing, key=int)) or "none"))
    return 0


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

    if "--check" in sys.argv:
        sys.exit(check_monitors(monitors))
    if "--scan-trac" in sys.argv:
        sys.exit(scan_trac())

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
        alerts = sweep(monitors, state, first_pass=(passes == 1))
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
