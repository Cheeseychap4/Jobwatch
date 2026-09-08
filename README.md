# jobwatch

Polls job sources every 15 minutes and pushes newly posted adverts to Telegram.
Standard library Python, runs free on GitHub Actions.

## Why it watches TRAC and not just NHS Jobs

Most NHS trusts recruit through **TRAC** (healthjobsuk.com). The advert opens
on TRAC first and is copied to **jobs.nhs.uk** afterwards — sometimes hours
later, sometimes days, and sometimes not until after the advert has already
closed on an application cap.

A Band 5 psychologist post was lost exactly that way: it opened on TRAC,
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
`towns`, `counties` and `employers` instead, because TRAC has no distance field.

## Adding a TRAC employer

Open any of that employer's job cards on healthjobsuk.com and look at the logo
image URL — `static.trac.jobs/employer-logos/476.png`. The number is the id.
Add it to `employer_ids`.

## Things worth knowing

- The national TRAC list cannot be sorted or deep-paged reliably. Coverage comes
  from the employer-id poller, never from that list.
- A new monitor name seeds silently on its first run — no flood of old adverts.
- The same job found on TRAC and later on NHS Jobs only alerts once.
- This is an **alerter, not a filter**. It deliberately over-includes; the job
  filter decides what is actually applicable.
