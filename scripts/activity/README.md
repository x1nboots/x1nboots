# Activity card

`generate.py` renders `assets/activity.svg` and `assets/activity-mobile.svg` from the
GitHub GraphQL API. `.github/workflows/activity.yml` runs it daily (04:23 UTC; GitHub
often starts scheduled runs later) with the built-in `GITHUB_TOKEN`, on every push that
changes this folder or the workflow, and on manual dispatch. It uses only the Python
standard library. An `ACTIVITY_TOKEN` environment variable, if set, takes precedence
over `GITHUB_TOKEN`. The workflow does not set one.

```sh
python3 -m unittest discover -s scripts/activity -v               # tests
python3 scripts/activity/generate.py --login x1nboots --out /tmp/card --from-json saved.json
```

## What the card measures

Every figure covers GitHub's contribution calendar for the past year: the 52 full weeks
plus the current one that GitHub returns by default, the same window as "contributions
in the last year" on the profile.

| Figure | Source |
| --- | --- |
| Contributions | `contributionCalendar.totalContributions`, checked against the sum of its days |
| Active days | Calendar days with at least one contribution |
| Longest streak | Longest run of consecutive active days inside the window |
| Current streak | Shown in the SVG description and the run log; see below |
| Commits | `totalCommitContributions` |
| Contributions per week | Sum of each Sunday-to-Saturday calendar week |
| Languages · public | Share of code size, see below |

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

## Languages

The bars show each language's share of the bytes GitHub Linguist detected across the
owner's public repositories. GitHub's language data already leaves out vendored,
generated and documentation files, and honours `linguist-*` overrides in
`.gitattributes`. Forks, archived repositories, mirrors and this profile repository are
skipped. Bytes measure how much code exists, not time spent writing it. A large
repository outweighs many small ones.

## What the card cannot see

- **Private contributions.** GitHub only exposes them, as anonymous daily counts, when
  *Settings → Public profile → Contributions & activity → Include private
  contributions on my profile* is on. When it is, the calendar figures include them
  and the description says how many there are. Repository names are never shown.
  Because other viewers get private activity only as an undifferentiated count
  (`restrictedContributionsCount`), the Commits figure should stay public-only.
- **Private repositories' languages.** `GITHUB_TOKEN` can only read this repository, so
  language data covers public repositories alone. Private repositories are filtered out
  in the query *and* again in code, so even a broader token cannot put them on the
  public card. Including them would need a separate fine-grained token with read-only
  *Metadata* access to those repositories, stored as a secret. That is a deliberate
  privacy trade-off, and it is not set up.

## Failure behaviour

The run fails, and the committed card stays as it was, when:

- an API request fails after its retries (5xx, rate limiting or network errors are
  retried twice; bad credentials are not);
- the API returns GraphQL errors or missing fields;
- the calendar has gaps, conflicting duplicate days, or a total that disagrees with
  its days;
- pagination does not advance;
- the calendar does not end within a day of the runner's UTC date, which indicates
  stale data;
- the rendered SVG is not well-formed;
- the tests fail.

Rendering is deterministic, so unchanged data produces byte-identical files and no
commit.

## Why the card can look unchanged

- Activity in private repositories does not appear (see above).
- GitHub serves the SVG with `Cache-Control: max-age=300`, so a refresh can take about
  five minutes to show. A browser may cache it longer.
- A run that found nothing new logs "Activity card unchanged." and commits nothing.
