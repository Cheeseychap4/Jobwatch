# jobwatch

Polls job sources every 15 minutes and pushes newly posted adverts to Telegram.
Standard library Python, runs free on GitHub Actions.

## Why it watches TRAC and not just NHS Jobs

Most NHS trusts recruit through **TRAC** (healthjobsuk.com). The advert opens
on TRAC first and is copied to **jobs.nhs.uk** afterwards — sometimes hours
later, sometimes days, and sometimes not until after the advert has already
closed on an application cap.

An Assistant Psychologist post was lost exactly that way: it opened on TRAC,
filled, and closed before any record of it reached NHS Jobs, so a monitor
watching NHS Jobs alone could never have seen it.

**TRAC is the source. NHS Jobs is the mirror. This monitor watches both, TRAC
first.** TRAC also publishes an explicit band on every card, which NHS Jobs
search results do not — so band filtering on the TRAC side is read, not guessed
from salary.

## Files

| File | What it does |
|---|---|
| `watch.py` | The monitor. No dependencies. |
| `monitors.json` | The searches. Edit this, not the code. |
| `state.json` | What has already been alerted on. Written by the workflow; don't edit. |
| `.github/workflows/jobwatch.yml` | The 15-minute schedule and the Telegram secrets. |

## Monitor types

Each entry in `monitors.json` has a `source`:

- `"trac_employers"` — polls each TRAC employer id in `employer_ids`. One short
  request per employer, complete coverage of that employer. **This is the one
  that catches fast-closing posts.**
- `"trac"` — the national TRAC list, used only to spot employers not yet in the
  id list. Any advert it finds from an unknown employer is flagged in the alert
  with the id to add.
- `"nhs"` — an NHS Jobs search URL, as before.
- `"feed"` — a private provider's own careers system (Cygnet, Practice Plus
  Group Health in Justice, St Andrew's). Private hospitals are not on TRAC and
  do not post everything to NHS Jobs, so each is read from its own feed. Same
  principle as TRAC: go to the system the employer actually recruits on.
- `"moj"` — the HMPPS / Ministry of Justice board, `jobs.justice.gov.uk`. Prison
  psychology-adjacent posts are never on NHS Jobs or TRAC at all, so without
  this source the whole justice side was only ever found by hand. One search per
  keyword in `keywords`, because the same job is a Group Worker at one prison
  and a Facilitator at the next.

There are two TRAC employer pollers, one for psychology and practitioner titles
and one for the support, HCA, recovery and peer-support tier, so the Reaside and
Ardenleigh Band 3 campaigns are covered as well as the Band 4/5 psychology ones.

Filters available on every monitor: `title_filter`, `exclude_title`, `bands`,
`allow_unknown_band`, `exclude_discipline`, `protect_title`, `max_salary`. NHS
monitors also take `max_miles`; TRAC, feed and MoJ monitors take `towns`,
`counties`, `employers` and `exclude_towns` instead, because those boards
publish a place, not a distance.

## Geography fails open, on purpose

On the employer poller every employer is already in radius, so an unrecognised
town is **kept and flagged** rather than dropped (`"unknown_location": "keep"`).
A post must never be lost because nobody had added the town to a list yet —
that is the exact failure this monitor exists to prevent. `exclude_towns` drops
the known out-of-area sites those trusts also run.

The national discovery monitor sets `"unknown_location": "drop"`, because there
an unrecognised town is almost certainly the other end of the country.

Town matching is whole-word, so "northampton" does not match "corby,
northamptonshire".

## Adding a TRAC employer

Open any of that employer's job cards on healthjobsuk.com and look at the logo
image URL — `static.trac.jobs/employer-logos/476.png`. The number is the id.
Add it to `employer_ids`.

## How often it really checks

A 15-minute cron does not fire every 15 minutes. GitHub throttles scheduled
workflows, and in practice this one fired every 45 to 90 minutes and skipped
overnight. Adding more cron lines does not help — the throttle is per repo.

So each firing now stays alive instead. `JOBWATCH_LOOP_SECONDS` (45 minutes)
and `JOBWATCH_INTERVAL_SECONDS` (5 minutes) make a single run sweep about nine
times, and because runs queue behind each other rather than cancelling, cover is
close to continuous. A pass takes roughly two minutes. State is written after
every pass, so a run that is cut short keeps what it has seen.

## Private providers do not publish a location

Cygnet's location field is a hospital name ("The Squirrels", "Tabley House"),
not a town, and Practice Plus Group's is a prison. A sitemap carries no location
at all. For those two the in-radius sites are listed by name and everything else
is dropped — an unknown value there carries no information, and failing open
would mean a hundred alerts a run. Both also post to NHS Jobs, where the
40-mile monitors apply a real distance filter.

The location is read by opening the advert itself, but only for an advert that
is both new and past the title filter, so it costs a few requests a run rather
than hundreds.

## Two things that would silently lose a post

**A TRAC employer page shows 50 vacancies and pages the rest.** Reading page 1
only would hide everything a large trust advertises beyond its fiftieth
vacancy — Midlands Partnership runs 79 and Oxford Health 60 today, and an
Assistant Psychologist was sitting on page 2. The poller follows the pages.

**The county in a TRAC advert URL is the employer's home county, not the
advert's location.** Every Midlands Partnership advert reads "Staffordshire"
whatever town it is in, so county is useless as a geography filter and no
monitor sets one. Towns do the work.

## Doing the same work once

One pass used to fetch the same 31 TRAC employer pages twice, because the
psychology poller and the support poller read the same employers. Pages are now
cached for the length of a pass and the cache is cleared between passes, so
coverage is unchanged and the request count is not. Every pass prints what it
fetched and what came from cache.

The national discovery sweep carries `"every_pass": false`: it exists to spot an
employer missing from the id list, which cannot meaningfully change inside one
45-minute run, so it runs on the first pass only.

Measured over a run of nine passes: **882 requests before, 494 after.**

## The justice board publishes a salary, not a band

HMPPS advertises a salary band (`£30,001 to £40,000`), no AfC band and no grade,
so band filtering cannot apply there. `max_salary` does the equivalent job: it
drops anything advertised above the Band 5 ceiling and keeps the rest, so a
Band 5-equivalent post is never lost for want of a band field.

Its location field is a Business Unit, which is often a service name
("Psychology Services") rather than a town, so the geography check reads the
**title** as well — prison names live in the title. A post whose location cannot
be resolved is kept and flagged `unknown` rather than dropped, on the same
fail-open principle as the TRAC employer poller.

Two monitors run against it: one on the house terms (`group worker`,
`offending behaviour`, `facilitator`, `psycholog`, `interventions`,
`programmes`, `case administrator`) and one on the in-radius establishments by
name (Onley, Rye Hill, Hewell, Birmingham, Featherstone, Brinsford, Oakwood,
Swinfen Hall).

The East Midlands cluster — Stocken, Whatton, Ranby, Gartree, and the Leicester
and Nottingham sites — is deliberately **not** in the exclusion list. Whether it
is commutable is an open question, so those posts alert as `unknown` rather than
disappearing. A geography-only cut is a near-miss, never a silent one.

The same house terms were added to the NHS and TRAC psychology searches at the
same time. `group worker` and `facilitator` are the best-paying seam and until
now the searches did not look for them at all.

## Other clinical disciplines

"Assistant" is also the word in Assistant Psychologist, so the grade can't be
filtered on. Two terms in the title filter — `assistant practitioner` and
`therapy assistant` — were dragging in every other profession's assistant grade:
occupational therapy, speech and language, urology, maternity. Both are gone;
anything psychology-related already matches `psycholog`.

Behind that, `exclude_discipline` names the other professions directly — OT,
SLT, physio, dietetics, podiatry, radiography, pharmacy, and the body-system
specialties. `protect_title` overrides it, so a title that says psychology,
forensic or mental health is never dropped by a discipline rule. That is what
keeps "Assistant Psychologist (maternity cover)" alive against a maternity
exclusion.

## Things worth knowing

- The national TRAC list cannot be sorted or deep-paged reliably. Coverage comes
  from the employer-id poller, never from that list.
- Every fetch is retried twice before it is allowed to fail. A dropped
  connection used to cost a whole employer or keyword for that pass, silently.
- A new monitor name seeds silently on its first run — no flood of old adverts.
- The same job found on TRAC and later on NHS Jobs only alerts once.
- This is an **alerter, not a filter**. It deliberately over-includes; the job
  filter decides what is actually applicable.
