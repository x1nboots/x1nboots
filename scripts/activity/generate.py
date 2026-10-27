#!/usr/bin/env python3
"""Render the profile activity card from public GitHub data.

Runs daily in GitHub Actions (.github/workflows/activity.yml) with the default
GITHUB_TOKEN and only the Python standard library. Every number on the card
comes straight from the GitHub GraphQL API. If a request fails or a response is
incomplete, the script exits non-zero and the previously committed card is left
untouched. What each number measures is documented in README.md next to this file.

Usage:
    python3 scripts/activity/generate.py --login x1nboots --out assets
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = "https://api.github.com/graphql"
RECENT_DAYS = 30

CONTRIBUTIONS_QUERY = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      totalCommitContributions
      restrictedContributionsCount
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""

# Palette shared with the static artwork in assets/.
NIGHT = "#0B0809"
INK = "#F3EAEC"
INK_2 = "#A8949A"
INK_3 = "#8C787E"
SAKURA = "#D8A1AE"
PETAL = ("M0 0C-1.6 -1.2 -5.2 -4 -4.9 -7.2C-4.7 -9.4 -2.6 -10.4 -1 -9.9L0 -8.7L1 -9.9"
         "C2.6 -10.4 4.7 -9.4 4.9 -7.2C5.2 -4 1.6 -1.2 0 0Z")
MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()


class DataError(Exception):
    """The GitHub API failed, or answered with data the card cannot trust."""


# ------------------------------------------------------------------ data
def graphql(token, query, variables, attempts=3):
    body = json.dumps({"query": query, "variables": variables}).encode()
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(API, data=body, headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-activity-card",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.load(resp)
            break
        except urllib.error.HTTPError as err:
            # 5xx and rate limits (403/429) can pass; bad credentials or queries will not
            if (err.code < 500 and err.code not in (403, 429)) or attempt == attempts:
                raise DataError(f"GitHub API request failed: HTTP {err.code} {err.reason}") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as err:
            if attempt == attempts:
                raise DataError(f"GitHub API request failed: {err}") from None
        time.sleep(2 ** attempt)
    if not isinstance(payload, dict):
        raise DataError("GitHub API returned an unexpected response")
    if payload.get("errors"):
        messages = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in payload["errors"])
        raise DataError(f"GitHub API returned errors: {messages}")
    return need(payload, "data")


def need(obj, *path):
    """obj[path[0]][path[1]]..., raising DataError if any step is missing or null."""
    for key in path:
        obj = obj.get(key) if isinstance(obj, dict) else None
        if obj is None:
            raise DataError(f"GitHub API response is missing {'.'.join(path)}")
    return obj


def fetch(login, token):
    return need(graphql(token, CONTRIBUTIONS_QUERY, {"login": login}), "user")


def calendar_days(calendar):
    """[(date, count)] for every calendar day, oldest first, once the calendar is known to be whole.

    GitHub returns one unbroken run of days. Exact duplicates are ignored; conflicting
    duplicates, gaps, or a total that disagrees with the days mean a partial response,
    which is rejected rather than guessed at.
    """
    counts = {}
    for week in need(calendar, "weeks"):
        for day in need(week, "contributionDays"):
            date = dt.date.fromisoformat(need(day, "date"))
            n = need(day, "contributionCount")
            if not isinstance(n, int) or n < 0:
                raise DataError(f"invalid contribution count on {date}")
            if counts.setdefault(date, n) != n:
                raise DataError(f"conflicting contribution counts for {date}")
    if not counts:
        raise DataError("contribution calendar is empty")
    first, last = min(counts), max(counts)
    if len(counts) != (last - first).days + 1:
        raise DataError(f"contribution calendar has gaps between {first} and {last}")
    if need(calendar, "totalContributions") != sum(counts.values()):
        raise DataError("contribution calendar total does not match its days")
    return sorted(counts.items())


def streaks(days):
    """(current, longest) runs of consecutive active days in an unbroken [(date, count)] calendar.

    The calendar's last day is today. Today does not break the current streak while
    it has no contributions yet; the streak then counts back from yesterday.
    """
    longest = run = before_today = 0
    for _, n in days:
        before_today = run
        run = run + 1 if n else 0
        longest = max(longest, run)
    current = run if days[-1][1] else before_today
    return current, longest


def recent(days, n=RECENT_DAYS):
    """(contributions, active days, first day) over the calendar's last n days, today included."""
    if len(days) < n:
        raise DataError(f"contribution calendar covers {len(days)} days, fewer than {n}")
    window = days[-n:]
    return sum(c for _, c in window), sum(1 for _, c in window if c), window[0][0]


def summarise(user):
    collection = need(user, "contributionsCollection")
    days = calendar_days(need(collection, "contributionCalendar"))
    current, longest = streaks(days)
    recent_total, recent_active, recent_first = recent(days)

    weeks = {}  # GitHub's calendar weeks run Sunday to Saturday
    for date, n in days:
        weeks.setdefault(date - dt.timedelta(days=(date.weekday() + 1) % 7), []).append((date, n))

    return {
        "total": sum(n for _, n in days),
        "private": need(collection, "restrictedContributionsCount"),
        "commits": need(collection, "totalCommitContributions"),
        "active_days": sum(1 for _, n in days if n),
        "current_streak": current,
        # a run reaching back to the first calendar day may have started before it
        "current_streak_open": 0 < current == len(days) - (not days[-1][1]),
        "longest_streak": longest,
        "first_day": days[0][0],
        "last_day": days[-1][0],
        "weeks": [(w[0][0], len(w), sum(n for _, n in w)) for w in weeks.values()],
        "recent_total": recent_total,
        "recent_active": recent_active,
        "recent_first": recent_first,
    }


def check_fresh(stats, today):
    """Reject a calendar that does not end today, allowing a day either way for time zones."""
    if abs((stats["last_day"] - today).days) > 1:
        raise DataError(f"contribution calendar ends {stats['last_day']}, expected {today}")


# ------------------------------------------------------------------ type
def fmt(v):
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Type:
    """Sets text with Jost outlines (glyphs.json) referenced through <use>."""

    def __init__(self, path):
        data = json.loads(path.read_text())
        self.styles = {k: data[k] for k in ("medium", "light")}
        self.used = {}

    def _layout(self, text, style, tracking):
        table = self.styles[style]
        glyphs, kerning = table["glyphs"], table["kerning"]
        chars = list(text.upper())
        pen, placed = 0, []
        for i, ch in enumerate(chars):
            g = glyphs.get(ch)
            if g and g["d"]:
                placed.append((ch, pen))
            pen += g["adv"] if g else 260
            if i + 1 < len(chars):
                pen += kerning.get(ch + chars[i + 1], 0) + tracking * 1000
        return placed, pen

    def width(self, text, style, size, tracking=0.0):
        return self._layout(text, style, tracking)[1] * size / 1000

    def run(self, text, style, size, x, y, fill, tracking=0.0, anchor="start"):
        placed, pen = self._layout(text, style, tracking)
        k = size / 1000
        if anchor == "middle":
            x -= pen * k / 2
        elif anchor == "end":
            x -= pen * k
        uses = []
        for ch, px in placed:
            ref = f"{style[0]}{ord(ch)}"
            self.used[ref] = self.styles[style]["glyphs"][ch]["d"]
            uses.append(f'<use href="#{ref}" x="{px:.0f}"/>')
        return f'<g transform="translate({fmt(x)} {fmt(y)}) scale({k:.5g})" fill="{fill}">{"".join(uses)}</g>'

    def defs(self):
        return "".join(f'<path id="{ref}" d="{d}"/>' for ref, d in sorted(self.used.items()))


# ------------------------------------------------------------------ drawing
def card(w, h, rx):
    return (
        f'<defs><clipPath id="clip"><rect width="{w}" height="{h}" rx="{rx}"/></clipPath>'
        f'<radialGradient id="glow" cx=".18" cy=".05" r=".95">'
        f'<stop offset="0" stop-color="#5A1528" stop-opacity=".42"/>'
        f'<stop offset=".55" stop-color="#2A0A14" stop-opacity=".19"/>'
        f'<stop offset="1" stop-color="{NIGHT}" stop-opacity="0"/></radialGradient>'
        f'<linearGradient id="rule" x1="0" x2="1"><stop offset="0" stop-color="{SAKURA}" stop-opacity=".3"/>'
        f'<stop offset="1" stop-color="{SAKURA}" stop-opacity="0"/></linearGradient>'
        f'<g id="bloom" fill="{SAKURA}">'
        + "".join(f'<use href="#pt" transform="rotate({i * 72})"/>' for i in range(5))
        + f'<circle r="2" fill="#7D2338"/></g><path id="pt" d="{PETAL}"/></defs>'
        f'<rect width="{w}" height="{h}" rx="{rx}" fill="{NIGHT}"/>'
        f'<rect width="{w}" height="{h}" fill="url(#glow)" clip-path="url(#clip)"/>'
        f'<rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="{rx - .5}" fill="none" '
        f'stroke="{SAKURA}" stroke-opacity=".16"/>'
    )


def kicker(t, text, x, y, size, rule_to=None):
    out = t.run(text, "medium", size, x, y, SAKURA, tracking=0.34)
    if rule_to:
        x0 = x + t.width(text, "medium", size, 0.34) + size * 1.4
        out += f'<rect x="{fmt(x0)}" y="{fmt(y - size * 0.38)}" width="{fmt(rule_to - x0)}" height="1" fill="url(#rule)"/>'
    return out


def figure(t, value, unit, label, x, y, size, label_size, label_gap):
    out = t.run(f"{value:,}", "light", size, x, y, INK)
    if unit:
        ux = x + t.width(f"{value:,}", "light", size) + size * 0.14
        out += t.run(unit, "medium", label_size, ux, y, INK_2, tracking=0.2)
    return out + t.run(label, "medium", label_size, x, y + label_gap, INK_2, tracking=0.24)


def figure_width(t, value, unit, label, size, label_size):
    top = t.width(f"{value:,}", "light", size)
    if unit:
        top += size * 0.14 + t.width(unit, "medium", label_size, 0.2)
    return max(top, t.width(label, "medium", label_size, 0.24))


def figures(t, items, x0, x1, y, label_gap, min_gap):
    """Lay out (value, unit, label, size, label_size) figures with equal gaps between x0 and x1."""
    widths = [figure_width(t, v, u, lab, sz, lsz) for v, u, lab, sz, lsz in items]
    gap = max((x1 - x0 - sum(widths)) / max(len(items) - 1, 1), min_gap)
    out, x = [], x0
    for (v, u, lab, sz, lsz), w in zip(items, widths):
        out.append(figure(t, v, u, lab, x, y, sz, lsz, label_gap))
        x += w + gap
    return "".join(out)


def timeline(t, weeks, x0, x1, base, height, marker, label_size, label_gap, month_every=1):
    """One lollipop per week: stem height = contributions that week."""
    peak = max((c for _, _, c in weeks), default=0)
    step = (x1 - x0) / max(len(weeks) - 1, 1)
    out = [f'<rect x="{fmt(x0 - 6)}" y="{fmt(base)}" width="{fmt(x1 - x0 + 12)}" height="1" '
           f'fill="{SAKURA}" fill-opacity=".22"/>']
    stems, dots, blooms, labels = [], [], [], []
    peak_done = False
    last_label_end = -1e9
    for i, (start, length, count) in enumerate(weeks):
        x = x0 + i * step
        if count:
            top = base - max(height * count / peak, marker * 2.2)
            stems.append(f"M{fmt(x)} {fmt(base)}V{fmt(top)}")
            blooms.append(f'<use href="#bloom" transform="translate({fmt(x)} {fmt(top)}) '
                          f'scale({fmt(marker / 10)}) rotate({(i * 23) % 72})"/>')
            if count == peak and not peak_done:
                peak_done = True
                labels.append(t.run(f"{count:,}", "medium", label_size, x, top - marker - label_size * 0.7,
                                    INK, anchor="middle"))
        else:
            dots.append(f'<circle cx="{fmt(x)}" cy="{fmt(base)}" r="{fmt(marker * 0.26)}"/>')
        # label the week that holds the 1st of a month, unless it would crowd its neighbour
        first = next((d for d in (start + dt.timedelta(days=k) for k in range(length)) if d.day == 1), None)
        if i and first and (first.month - 1) % month_every == 0:
            name = MONTHS[first.month - 1]
            w = t.width(name, "medium", label_size, 0.2)
            if x - w / 2 > last_label_end + label_size and x + w / 2 <= x1 + 6:
                labels.append(t.run(name, "medium", label_size, x, base + label_gap, INK_3,
                                    tracking=0.2, anchor="middle"))
                last_label_end = x + w / 2
    out.append(f'<g fill="{INK_3}" fill-opacity=".6">{"".join(dots)}</g>')
    out.append(f'<path d="{"".join(stems)}" stroke="{SAKURA}" stroke-opacity=".75" '
               f'stroke-width="{fmt(marker * 0.34)}" stroke-linecap="round" fill="none"/>')
    out += blooms + labels
    return "".join(out)


def plural(n, word):
    return f"{n:,} {word}" if n == 1 else f"{n:,} {word}s"


def describe(s):
    private = f" (including {s['private']:,} private, as counts only)" if s["private"] else ""
    current = ("at least " if s["current_streak_open"] else "") + plural(s["current_streak"], "day")
    return (f"{plural(s['total'], 'contribution')}{private} in the past year on {plural(s['active_days'], 'day')}; "
            f"current streak {current}; longest streak {plural(s['longest_streak'], 'day')}; "
            f"{plural(s['commits'], 'commit')}. Last {RECENT_DAYS} days, today included: "
            f"{plural(s['recent_total'], 'contribution')} on {plural(s['recent_active'], 'day')}. "
            f"Private contributions count only when GitHub shows them on the public profile.")


def render(s, glyphs, mobile=False):
    t = Type(glyphs)
    streak_unit = "DAY" if s["longest_streak"] == 1 else "DAYS"
    recent_kicker = f"LAST {RECENT_DAYS} DAYS"
    if not mobile:
        w, h, rx = 1200, 470, 20
        body = [
            kicker(t, "THE PAST YEAR", 56, 78, 13.5, rule_to=790),
            figures(t, [(s["total"], None, "CONTRIBUTIONS", 84, 13),
                        (s["active_days"], None, "ACTIVE DAYS", 48, 13),
                        (s["longest_streak"], streak_unit, "LONGEST STREAK", 48, 13),
                        (s["commits"], None, "COMMITS", 48, 13)], 56, 790, 166, 34, 36),
            # a hairline in the border's colour sets the 30-day figures apart from the year's
            f'<rect x="822" y="64" width="1" height="142" fill="{SAKURA}" fill-opacity=".16"/>',
            kicker(t, recent_kicker, 854, 78, 13.5),
            figures(t, [(s["recent_total"], None, "CONTRIBUTIONS", 48, 13),
                        (s["recent_active"], None, "ACTIVE DAYS", 48, 13)], 854, 1144, 166, 34, 36),
            kicker(t, "CONTRIBUTIONS PER WEEK", 56, 268, 13.5, rule_to=1144),
            timeline(t, s["weeks"], 64, 1136, 406, 96, 6, 12, 30),
        ]
    else:
        w, h, rx = 720, 1044, 24
        body = [
            kicker(t, "THE PAST YEAR", 52, 92, 19, rule_to=668),
            figure(t, s["total"], None, "CONTRIBUTIONS", 52, 236, 128, 18, 46),
            figures(t, [(s["active_days"], None, "ACTIVE DAYS", 64, 16),
                        (s["longest_streak"], streak_unit, "LONGEST STREAK", 64, 16),
                        (s["commits"], None, "COMMITS", 64, 16)], 52, 668, 392, 40, 28),
            kicker(t, "CONTRIBUTIONS PER WEEK", 52, 510, 19, rule_to=668),
            timeline(t, s["weeks"], 60, 660, 720, 140, 5.2, 16, 40, month_every=2),
            kicker(t, recent_kicker, 52, 840, 19, rule_to=668),
            figures(t, [(s["recent_total"], None, "CONTRIBUTIONS", 64, 16),
                        (s["recent_active"], None, "ACTIVE DAYS", 64, 16)], 52, 668, 936, 40, 28),
        ]
    content = "".join(body)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        f'role="img" aria-labelledby="t d"><!-- Generated by scripts/activity/generate.py from the GitHub API. '
        f'Type: Jost (SIL Open Font License 1.1), as outlines. --><title id="t">GitHub activity</title>'
        f'<desc id="d">{esc(describe(s))}</desc>{card(w, h, rx)}<defs>{t.defs()}</defs>{content}</svg>\n'
    )


def validate(svg):
    """Raise DataError unless the SVG is well-formed and every <use> points at a defined id."""
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as err:
        raise DataError(f"generated SVG is not well-formed: {err}") from None
    ids = {el.get("id") for el in root.iter()}
    refs = {el.get("href")[1:] for el in root.iter() if (el.get("href") or "").startswith("#")}
    if refs - ids:
        raise DataError(f"generated SVG references undefined ids: {sorted(refs - ids)}")


def write(path, text):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--login", required=True)
    ap.add_argument("--out", default="assets", type=Path)
    ap.add_argument("--from-json", type=Path, help="render from a saved API response instead of fetching")
    args = ap.parse_args(argv)

    try:
        if args.from_json:
            user = json.loads(args.from_json.read_text())
        else:
            token = os.environ.get("ACTIVITY_TOKEN") or os.environ.get("GITHUB_TOKEN")
            if not token:
                raise SystemExit("Set GITHUB_TOKEN (or ACTIVITY_TOKEN) to query the GitHub API.")
            user = fetch(args.login, token)
        stats = summarise(user)
        if not args.from_json:
            check_fresh(stats, dt.datetime.now(dt.timezone.utc).date())
        glyphs = HERE / "glyphs.json"
        cards = {"activity.svg": render(stats, glyphs), "activity-mobile.svg": render(stats, glyphs, mobile=True)}
        for svg in cards.values():
            validate(svg)
    except DataError as err:
        raise SystemExit(f"Activity card not updated: {err}")

    args.out.mkdir(parents=True, exist_ok=True)
    for name, svg in cards.items():
        write(args.out / name, svg)
    print(describe(stats))
    print(f"Calendar {stats['first_day']} to {stats['last_day']} as dated by GitHub for this token; "
          f"last {RECENT_DAYS} days from {stats['recent_first']}.")


if __name__ == "__main__":
    sys.exit(main())
