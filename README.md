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

Filters available on every monitor: `title_filter`, `exclude_title`, `bands`,
`allow_unknown_band`. NHS monitors also take `max_miles`; TRAC monitors take
`towns`, `counties`, `employers` and `exclude_towns` instead, because TRAC
publishes a town, not a distance.

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

## Two things that would silently lose a post

**A TRAC employer page shows 50 vacancies and pages the rest.** Reading page 1
only would hide everything a large trust advertises beyond its fiftieth
vacancy — Midlands Partnership runs 79 and Oxford Health 60 today, and an
Assistant Psychologist was sitting on page 2. The poller follows the pages.

**The county in a TRAC advert URL is the employer's home county, not the
advert's location.** Every Midlands Partnership advert reads "Staffordshire"
whatever town it is in, so county is useless as a geography filter and
`exclude_counties` is deliberately left empty. Towns do the work.

## Things worth knowing

- The national TRAC list cannot be sorted or deep-paged reliably. Coverage comes
  from the employer-id poller, never from that list.
- A new monitor name seeds silently on its first run — no flood of old adverts.
- The same job found on TRAC and later on NHS Jobs only alerts once.
- This is an **alerter, not a filter**. It deliberately over-includes; the job
  filter decides what is actually applicable.
