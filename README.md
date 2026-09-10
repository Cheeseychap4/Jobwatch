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
| `profile.json` | The screening rules. Edit this, not the code. |
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
- `"wmjobs"` — wmjobs.co.uk, the shared board most West Midlands councils
  advertise on. The councils are the one seam neither the NHS nor the justice
  monitors can see, and they are where the income-fork roles actually live —
  information rights, business support, residential care. One search per
  keyword in `keywords`.
- `"tribepad"` — a council running its own applicant tracking system, which is
  then the source and wmjobs only the mirror. Coventry City Council is the one
  in scope. The cards carry contract type, closing date and posting date, so
  nothing has to be inferred, and paging is stateless (`/jobs/search/-1/<n>`),
  so the whole list is read rather than just the first page.
- `"smartrecruiters"` — a charity or company recruiting through SmartRecruiters,
  read from its public JSON API by `company` name. No HTML, so no layout to
  break. These are national employers that always publish a city, so the
  monitors set `"unknown_location": "drop"`: an unrecognised town here is a
  genuinely distant job, not a missing field.
- `"feed"` also takes `"feed_kind": "links"` — an ordinary HTML page that links
  out to each vacancy. For an applicant tracking system that renders through
  JavaScript and cannot be read directly, but whose adverts are linked from the
  charity's own site. Title comes from the URL slug, location from a body probe.

There are two TRAC employer pollers, one for psychology and practitioner titles
and one for the support, HCA, recovery and peer-support tier, so the Reaside and
Ardenleigh Band 3 campaigns are covered as well as the Band 4/5 psychology ones.

Filters available on every monitor: `title_filter`, `exclude_title`, `bands`,
`allow_unknown_band`, `exclude_discipline`, `protect_title`, `max_salary`,
`min_salary`, `exclude_employers`, `contract_filter`, `exclude_contract`. NHS
monitors also take `max_miles`; TRAC, feed, MoJ, wmjobs and Tribepad monitors
take `towns`, `counties`, `employers` and `exclude_towns` instead, because those
boards publish a place, not a distance.

`min_salary` is the income fork's floor and tests the **top** of an advertised
range, so a band that starts below the floor and finishes above it is kept and
the split is left visible rather than cut silently. `contract_filter` and
`exclude_contract` only bite where the board actually prints a contract type —
wmjobs does not, so they fail open there rather than losing an advert to a field
that was simply absent.

`probe_body` (wmjobs only) fetches the advert itself for the fields the search
card leaves out — contract type, hours, real closing date and the full salary
line. It runs after the cheap filters, so it costs one fetch per genuinely new
advert rather than one per advert seen. **A contract filter on wmjobs without
`probe_body` is worse than no filter**, because it silently never fires; the
`--check` mode below refuses that combination.

`exclude_employers` matches on the employer name rather than the location, and
does two jobs on a shared council board. It keeps out authorities that are
plainly out of region but whose adverts give a venue name ("Wildwood", "The
Guildhall, Frankwell") that no town list can recognise. And it enforces the
standing block on policing employers, which matters because a Police and Crime
Commissioner's business-support advert sits on wmjobs looking like any other
council post.

## What an alert looks like

Two lines. The role, hyperlinked to the advert, and where it is:

> **[Information Rights Officer](#)**
> Warwickshire County Council - Warwick, Warwickshire

Band, salary, contract type and which source found it are all one tap away on
the advert, and a ping exists to decide whether to open it. The only thing added
back is a third line when the location could not be matched against the town
list - that is the alert saying it could not do the geography for you, and it is
the one omission that could cost a job.

### NHS Jobs alerts also carry the person specification

NHS Jobs numbers every criterion in the markup
(`essential_skill_N_criteria_M`), so the essential criteria can be read from the
advert rather than guessed at. Those alerts get two extra parts:

A **screen line**, which is a keyword check of the criteria against
`profile.json`:

> Screen: blocked - 2:1 degree essential (BSc is a 2:2); own vehicle required

And a **code block** holding the role, the pay, the closing date, the link and
the essential criteria. Telegram copies a code block on a tap, so the whole
advert can be pasted into a fit assessment without opening anything:

> ```
> Assistant Psychologist
> Coventry and Warwickshire Partnership NHS Trust, Coventry
> £28,392 to £31,157 a year
> The closing date is 25 September 2026
> https://www.jobs.nhs.uk/candidate/jobadvert/C9820-26-0621
>
> Essential criteria:
> [X] Qualifications: Honours degree in Psychology at 2:1 or above
> [?] Experience: Experience of working with people with complex needs
> - Skills: Ability to write clear clinical notes
> (+6 more on the advert)
>
> [X] blocker   [?] worth checking
> ```

The block is capped at nine criteria and about 950 characters, so it stays
readable on a phone. Anything a rule fired on is kept first, then qualifications
and experience, then the rest in advert order; whatever gets dropped is counted
rather than quietly binned.

The screen is a keyword match, not a judgement. It exists to kill the obvious
non-starters before they cost a tap. Three states:

- `Screen: blocked` - an essential criterion that cannot currently be met.
- `Screen: check` - worth a second look, not a refusal.
- `Screen: no flags` - nothing in the rule list fired. Not the same as a good fit.
- `Screen: spec not on NHS Jobs` - the trust put the person spec in an attached
  document and left a "click apply" placeholder behind. Nothing was screened,
  and the alert says so rather than reporting a clean advert.

TRAC serves its advert pages behind Cloudflare and refuses datacentre traffic,
so TRAC alerts stay at two lines. The council, charity and justice boards each
use their own markup and would need a parser each; they stay at two lines too.

### Editing the screening rules

`profile.json` holds the rules as plain text, so they can be changed without
touching Python. Each rule is:

```json
{
  "level": "block",
  "reason": "own vehicle required (licence yes, no car)",
  "any": ["access to a car", "own transport"],
  "unless": ["desirable"]
}
```

`any` fires the rule if a criterion contains any of those phrases. `unless`
keeps it quiet if the criterion also contains one of those - that is what stops
"six months experience, paid or voluntary" from being read as a hard gate.
Matching ignores case and punctuation, so `2:1` also catches `2.1`. Patterns of
four characters or fewer have to be whole words, so `bps` does not match inside
another word.

## Command-line modes

```
python watch.py              # one pass, or a timed loop (see env vars below)
python watch.py --check      # validate monitors.json, no network
python watch.py --scan-trac  # probe every TRAC employer id for in-radius adverts
```

`--check` compiles every regex in `monitors.json` and verifies each monitor has
the keys its source needs. Worth knowing why: filters are compiled at match
time, so a stray bracket does not fail loudly — it raises inside one monitor's
loop and that monitor quietly stops matching. Run it before every deploy.

`--scan-trac` exists because the TRAC employer id list is a snapshot. An
employer that adopts TRAC after the last scan is invisible until the next one.
Run it monthly and add any id it marks `NEW`.

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
