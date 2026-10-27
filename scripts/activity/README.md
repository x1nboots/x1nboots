# Activity card

`generate.py` renders `assets/activity.svg` and `assets/activity-mobile.svg` from the
GitHub GraphQL API. `.github/workflows/activity.yml` runs it daily (04:23 UTC; GitHub
often starts scheduled runs later) with the built-in `GITHUB_TOKEN`, on every push that
changes this folder or the workflow, and on manual dispatch. It uses only the Python
standard library and makes a single GraphQL request for the contribution calendar.
An `ACTIVITY_TOKEN` environment variable, if set, takes precedence over
`GITHUB_TOKEN`. The workflow does not set one.

```sh
python3 -m unittest discover -s scripts/activity -v               # tests
python3 scripts/activity/generate.py --login x1nboots --out /tmp/card --from-json saved.json
```

## What the card measures

Every figure comes from GitHub's contribution calendar for the past year: the 52 full
weeks plus the current one that GitHub returns by default, the same window as
"contributions in the last year" on the profile.

| Section | Figure | Source |
| --- | --- | --- |
| The past year | Contributions | `contributionCalendar.totalContributions`, checked against the sum of its days |
| | Active days | Calendar days with at least one contribution |
| | Longest streak | Longest run of consecutive active days inside the window |
| | Current streak | Shown in the SVG description and the run log; see below |
| | Commits | `totalCommitContributions` |
| Last 30 days | Contributions | Sum of the last 30 calendar days, see below |
| | Active days | Days among those 30 with at least one contribution |
| Contributions per week | | Sum of each Sunday-to-Saturday calendar week |

Programming languages are presented in the profile's Tech & Tools section and are not
measured by this card.

GitHub decides which contributions count. Commits count only when they are on a
repository's default branch and authored with an email address linked to the account.
The workflow's own "Refresh activity card" commits are authored by `github-actions[bot]`,
so they never count as the owner's activity.

## Streaks

- A day is active when GitHub's calendar reports one or more contributions for it.
- Days are the calendar's own dates. GitHub dates contributions in the time zone it
  associates with the requesting token. For the workflow's `GITHUB_TOKEN` that matches
  the public, logged-out profile, which uses UTC. The owner's personal token can date
  the same contribution a day later or earlier, so local runs with one may differ by
  a day around midnight.
- "Today" is the calendar's last day, never the runner's clock. While today has no
  contributions yet, the current streak counts back from yesterday. It resets to zero
  once a whole calendar day passes without a contribution.
- Streaks are measured inside the one-year window only, and the API refuses longer
  windows. A current streak that reaches the first day of the window is reported as
  "at least N days" because it may have started earlier. Nothing before the window is
  inferred.
- Leap days and year boundaries need no special handling, because days are compared as
  real dates.

## Last 30 days

- The window is the calendar's last day ("today", as for streaks) and the 29 days before
  it: exactly 30 calendar days, today included. Today counts even though it is not over;
  contributions made later in the day appear at the next run.
- It uses the same validated calendar and day boundaries as every other figure. No
  extra request is made.
- Month ends, year ends and February 29 need no special handling, because days are
  counted as real dates. For example, a window ending on 1 March 2028 starts on
  1 February, while one ending on 1 March 2027 starts on 31 January.
- A calendar covering fewer than 30 days is incomplete, so the run fails instead of
  showing a figure for a shorter period.

## What the card cannot see

**Private contributions are included only when GitHub exposes them.** GitHub shows
them, as anonymous daily counts, only while *Settings → Public profile → Contributions
& activity → Include private contributions on my profile* is on. When it is, every
calendar-based figure includes them: both contribution counts, both active-day counts,
the streaks and the weekly chart. The description then says how many there are.
Repository names and details are never available, and never shown. The Commits figure
stays public-only, because other viewers get private activity only as an
undifferentiated count (`restrictedContributionsCount`).

## Failure behaviour

The run fails, and the committed card stays as it was, when:

- an API request fails after its retries (5xx, rate limiting or network errors are
  retried twice; bad credentials are not);
- the API returns GraphQL errors or missing fields;
- the calendar has gaps, conflicting duplicate days, or a total that disagrees with
  its days;
- the calendar covers fewer than 30 days;
- the calendar does not end within a day of the runner's UTC date, which indicates
  stale data;
- the rendered SVG is not well-formed;
- the tests fail.

Rendering is deterministic, so unchanged data produces byte-identical files and no
commit.

## Why the card can look unchanged

- Private activity appears only while the profile setting above is on.
- GitHub serves the SVG with `Cache-Control: max-age=300`, so a refresh can take about
  five minutes to show. A browser may cache it longer.
- A run that found nothing new logs "Activity card unchanged." and commits nothing.
