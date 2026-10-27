"""Regression tests for the activity card generator (standard library only).

Run from the repository root:
    python3 -m unittest discover -s scripts/activity -v
"""
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate as g  # noqa: E402

LOGIN = "octo"
GLYPHS = g.HERE / "glyphs.json"


def calendar(start, counts):
    """GitHub-shaped calendar: Sunday-first weeks starting at `start` (a Sunday)."""
    days = [{"date": (start + dt.timedelta(days=i)).isoformat(), "contributionCount": n}
            for i, n in enumerate(counts)]
    weeks = [{"contributionDays": days[i:i + 7]} for i in range(0, len(days), 7)]
    return {"totalContributions": sum(counts), "weeks": weeks}


def user(counts, start=dt.date(2025, 10, 26), commits=None, restricted=0):
    return {
        "contributionsCollection": {
            "totalCommitContributions": sum(counts) if commits is None else commits,
            "restrictedContributionsCount": restricted,
            "contributionCalendar": calendar(start, counts),
        },
    }


def user_ending(last, active, length=371):
    """A calendar of about a year that ends on `last`, with {date: count} contributions."""
    start = last - dt.timedelta(days=length - 1)
    start -= dt.timedelta(days=(start.weekday() + 1) % 7)  # back to a Sunday, as GitHub starts
    counts = [active.get(start + dt.timedelta(days=i), 0) for i in range((last - start).days + 1)]
    return user(counts, start=start)


def days(counts, start=dt.date(2025, 10, 26)):
    return [(start + dt.timedelta(days=i), n) for i, n in enumerate(counts)]


def stats(counts, **kw):
    return g.summarise(user(counts, **kw))


def calendar_days_of(u):
    return u["contributionsCollection"]["contributionCalendar"]["weeks"][0]["contributionDays"]


class Characterisation(unittest.TestCase):
    """Behaviour of the earlier generator that must survive the changes."""

    def test_totals_active_days_and_longest_streak(self):
        counts = [0] * 367
        for i in (10, 11, 12, 40, 41, 300):
            counts[i] = 2
        s = g.summarise(user(counts, commits=9))
        self.assertEqual((s["total"], s["commits"], s["active_days"], s["longest_streak"]), (12, 9, 6, 3))

    def test_weeks_follow_github_calendar_weeks(self):
        s = stats([1] * 31)
        self.assertEqual(len(s["weeks"]), 5)
        self.assertEqual(s["weeks"][0], (dt.date(2025, 10, 26), 7, 7))
        self.assertEqual(s["weeks"][-1], (dt.date(2025, 11, 23), 3, 3))

    def test_render_keeps_card_dimensions(self):
        s = stats([1] * 30)
        self.assertIn('viewBox="0 0 1200 470"', g.render(s, GLYPHS))
        self.assertIn('viewBox="0 0 720 1044"', g.render(s, GLYPHS, mobile=True))

    def test_render_is_deterministic(self):
        s = stats([0, 3, 1] * 100)
        self.assertEqual(g.render(s, GLYPHS), g.render(s, GLYPHS))


class Streaks(unittest.TestCase):
    def test_active_today_counts(self):
        self.assertEqual(g.streaks(days([0, 0, 1, 1])), (2, 2))

    def test_no_contribution_today_keeps_yesterdays_streak(self):
        self.assertEqual(g.streaks(days([0, 1, 1, 1, 0])), (3, 3))

    def test_no_contribution_today_or_yesterday_is_zero(self):
        self.assertEqual(g.streaks(days([1, 1, 1, 0, 0])), (0, 3))

    def test_across_midnight(self):
        # Contributions late on Mon Oct 27 and early on Tue Oct 28 fall on consecutive calendar days.
        week = [0] * 7
        week[1], week[2] = 3, 1  # calendar starts Sun Oct 26
        self.assertEqual(g.streaks(days(week[:3])), (2, 2))
        # The next run sees Wednesday, still empty: the streak survives until Wednesday is over ...
        self.assertEqual(g.streaks(days(week[:4])), (2, 2))
        # ... and is gone once Thursday is today.
        self.assertEqual(g.streaks(days(week[:5])), (0, 2))

    def test_broken_streak(self):
        self.assertEqual(g.streaks(days([1, 1, 1, 0, 1, 1])), (2, 3))
        self.assertEqual(g.streaks(days([1, 1, 0, 1, 1, 1, 1, 0, 1])), (1, 4))

    def test_year_boundary(self):
        # Sun Dec 28 2025 .. Fri Jan 2 2026
        self.assertEqual(g.streaks(days([0, 0, 1, 2, 3, 1], start=dt.date(2025, 12, 28))), (4, 4))
        s = g.summarise(user_ending(dt.date(2026, 1, 2), {dt.date(2025, 12, 30): 1, dt.date(2025, 12, 31): 2,
                                                          dt.date(2026, 1, 1): 3, dt.date(2026, 1, 2): 1}))
        self.assertEqual((s["current_streak"], s["longest_streak"], s["active_days"]), (4, 4, 4))
        self.assertEqual(s["weeks"][-1], (dt.date(2025, 12, 28), 6, 7))

    def test_leap_day(self):
        # Sun Feb 27 2028 .. Wed Mar 1 2028 passes through Feb 29.
        self.assertEqual(g.streaks(days([0, 1, 1, 1], start=dt.date(2028, 2, 27))), (3, 3))
        # Sun Dec 26 2027 .. Sat Dec 30 2028: every day of leap year 2028 is one unbroken run.
        s = stats([1] * 371, start=dt.date(2027, 12, 26))
        self.assertEqual(s["longest_streak"], 371)

    def test_non_leap_february_is_contiguous(self):
        # Sun Feb 28 2027 -> Mon Mar 1 2027
        self.assertEqual(g.streaks(days([1, 1], start=dt.date(2027, 2, 28))), (2, 2))

    def test_streak_reaching_window_start_is_marked_open(self):
        s = stats([1] * 30)
        self.assertTrue(s["current_streak_open"])
        self.assertIn("current streak at least 30 days", g.describe(s))
        self.assertTrue(stats([1] * 29 + [0])["current_streak_open"])
        self.assertFalse(stats([0] + [1] * 29)["current_streak_open"])
        self.assertFalse(stats([0] * 30)["current_streak_open"])

    def test_empty_activity(self):
        s = stats([0] * 367)
        self.assertEqual((s["total"], s["active_days"], s["current_streak"], s["longest_streak"]), (0, 0, 0, 0))
        self.assertIn("current streak 0 days; longest streak 0 days", g.describe(s))
        for mobile in (False, True):
            g.validate(g.render(s, GLYPHS, mobile=mobile))


class RecentWindow(unittest.TestCase):
    """The LAST 30 DAYS figures: today and the 29 calendar days before it."""

    TODAY = dt.date(2026, 10, 27)

    def recent(self, active, today=TODAY):
        s = g.summarise(user_ending(today, active))
        return s["recent_total"], s["recent_active"], s["recent_first"]

    def test_window_is_today_and_the_29_days_before(self):
        first = self.TODAY - dt.timedelta(days=29)
        active = {self.TODAY: 1, first: 2, first - dt.timedelta(days=1): 50}
        self.assertEqual(self.recent(active), (3, 2, first))

    def test_today_without_contributions_is_still_in_the_window(self):
        yesterday = self.TODAY - dt.timedelta(days=1)
        self.assertEqual(self.recent({yesterday: 4}), (4, 1, dt.date(2026, 9, 28)))

    def test_window_rolls_forward_one_day_per_calendar_day(self):
        oldest = self.TODAY - dt.timedelta(days=29)
        active = {oldest: 5, self.TODAY: 1}
        self.assertEqual(self.recent(active)[:2], (6, 2))
        tomorrow = self.TODAY + dt.timedelta(days=1)
        self.assertEqual(self.recent(active, today=tomorrow)[:2], (1, 1))

    def test_month_boundary(self):
        # Non-leap 2026: Jan 31 + all 28 days of February + Mar 1 make exactly 30 days.
        active = {dt.date(2026, 1, 30): 9, dt.date(2026, 1, 31): 1, dt.date(2026, 2, 14): 2, dt.date(2026, 3, 1): 3}
        self.assertEqual(self.recent(active, today=dt.date(2026, 3, 1)), (6, 3, dt.date(2026, 1, 31)))

    def test_year_boundary(self):
        active = {dt.date(2025, 12, 15): 9, dt.date(2025, 12, 16): 1, dt.date(2025, 12, 31): 1,
                  dt.date(2026, 1, 1): 1, dt.date(2026, 1, 14): 1}
        self.assertEqual(self.recent(active, today=dt.date(2026, 1, 14)), (4, 4, dt.date(2025, 12, 16)))

    def test_leap_year_february(self):
        # Leap 2028: Feb 1 .. Feb 29 + Mar 1 make 30 days, so Jan 31 falls outside ...
        active = {dt.date(2028, 1, 31): 9, dt.date(2028, 2, 1): 1, dt.date(2028, 2, 29): 2, dt.date(2028, 3, 1): 1}
        self.assertEqual(self.recent(active, today=dt.date(2028, 3, 1)), (4, 3, dt.date(2028, 2, 1)))
        # ... whereas in non-leap 2027 the same window reaches back to Jan 31.
        active = {dt.date(2027, 1, 30): 9, dt.date(2027, 1, 31): 1, dt.date(2027, 3, 1): 1}
        self.assertEqual(self.recent(active, today=dt.date(2027, 3, 1)), (2, 2, dt.date(2027, 1, 31)))

    def test_zero_activity(self):
        self.assertEqual(self.recent({})[:2], (0, 0))

    def test_only_older_activity(self):
        s = g.summarise(user_ending(self.TODAY, {dt.date(2026, 5, 1): 7, dt.date(2026, 9, 27): 1}))
        self.assertEqual((s["total"], s["recent_total"], s["recent_active"]), (8, 0, 0))

    def test_calendar_shorter_than_the_window_is_rejected(self):
        g.recent(days([1] * 30))
        with self.assertRaisesRegex(g.DataError, "29 days, fewer than 30"):
            g.recent(days([1] * 29))
        with self.assertRaisesRegex(g.DataError, "fewer than 30"):
            stats([1] * 20)

    def test_missing_day_inside_the_window_is_rejected(self):
        u = user([1] * 35)
        del u["contributionsCollection"]["contributionCalendar"]["weeks"][3]["contributionDays"][2]
        u["contributionsCollection"]["contributionCalendar"]["totalContributions"] = 34
        with self.assertRaisesRegex(g.DataError, "gaps"):
            g.summarise(u)

    def test_described_and_independent_of_yearly_figures(self):
        s = g.summarise(user_ending(self.TODAY, {dt.date(2026, 3, 1): 10, self.TODAY: 1}))
        self.assertEqual((s["total"], s["active_days"], s["recent_total"], s["recent_active"]), (11, 2, 1, 1))
        self.assertIn("Last 30 days, today included: 1 contribution on 1 day.", g.describe(s))


class CalendarValidation(unittest.TestCase):
    def test_duplicate_day_is_counted_once(self):
        u = user([1, 2, 3] + [0] * 32)
        calendar_days_of(u).append(dict(calendar_days_of(u)[1]))
        s = g.summarise(u)
        self.assertEqual((s["total"], s["active_days"], s["weeks"][0]), (6, 3, (dt.date(2025, 10, 26), 7, 6)))

    def test_conflicting_duplicate_day_is_rejected(self):
        u = user([1, 2, 3] + [0] * 32)
        calendar_days_of(u).append({"date": calendar_days_of(u)[1]["date"], "contributionCount": 7})
        with self.assertRaisesRegex(g.DataError, "conflicting"):
            g.summarise(u)

    def test_missing_days_are_rejected_not_bridged(self):
        u = user([1, 1, 0, 1, 1] + [0] * 30)
        del calendar_days_of(u)[2]
        with self.assertRaisesRegex(g.DataError, "gaps"):
            g.summarise(u)

    def test_missing_week_is_rejected(self):
        u = user([1] * 35)
        del u["contributionsCollection"]["contributionCalendar"]["weeks"][1]
        with self.assertRaisesRegex(g.DataError, "gaps"):
            g.summarise(u)

    def test_total_must_match_days(self):
        u = user([1, 2] + [0] * 30)
        u["contributionsCollection"]["contributionCalendar"]["totalContributions"] = 5
        with self.assertRaisesRegex(g.DataError, "total"):
            g.summarise(u)

    def test_empty_calendar_is_rejected(self):
        u = user([0] * 30)
        u["contributionsCollection"]["contributionCalendar"]["weeks"] = []
        with self.assertRaisesRegex(g.DataError, "empty"):
            g.summarise(u)

    def test_partial_response_is_rejected(self):
        for path in (("contributionsCollection",), ("contributionsCollection", "contributionCalendar"),
                     ("contributionsCollection", "totalCommitContributions"),
                     ("contributionsCollection", "restrictedContributionsCount")):
            u = user([1, 2] + [0] * 30)
            parent = u
            for key in path[:-1]:
                parent = parent[key]
            parent[path[-1]] = None
            with self.subTest(path=path), self.assertRaisesRegex(g.DataError, "missing"):
                g.summarise(u)

    def test_missing_day_count_is_rejected(self):
        u = user([1, 2] + [0] * 30)
        del calendar_days_of(u)[0]["contributionCount"]
        with self.assertRaisesRegex(g.DataError, "missing contributionCount"):
            g.summarise(u)

    def test_negative_count_is_rejected(self):
        u = user([1, 2] + [0] * 30)
        calendar_days_of(u)[0]["contributionCount"] = -1
        u["contributionsCollection"]["contributionCalendar"]["totalContributions"] = 1
        with self.assertRaisesRegex(g.DataError, "invalid"):
            g.summarise(u)

    def test_stale_calendar_is_rejected(self):
        s = stats([1] * 30)  # ends Mon Nov 24 2025
        g.check_fresh(s, dt.date(2025, 11, 24))
        g.check_fresh(s, dt.date(2025, 11, 25))  # runner already past midnight UTC
        g.check_fresh(s, dt.date(2025, 11, 23))  # token dated ahead of UTC
        with self.assertRaisesRegex(g.DataError, "ends 2025-11-24"):
            g.check_fresh(s, dt.date(2025, 11, 27))


class Description(unittest.TestCase):
    def test_private_contribution_counts_are_labelled(self):
        s = stats([2, 3] + [0] * 28, restricted=4)
        self.assertIn("5 contributions (including 4 private, as counts only)", g.describe(s))

    def test_private_visibility_is_always_stated(self):
        self.assertIn("Private contributions count only when GitHub shows them on the public profile.",
                      g.describe(stats([0] * 30)))

    def test_languages_are_no_longer_described(self):
        self.assertNotIn("anguage", g.describe(stats([1] * 30)))


class FakeAPI:
    """Stands in for urllib.request.urlopen, answering the contributions query."""

    def __init__(self, contributions):
        self.contributions = contributions
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(json.loads(req.data))
        return io.BytesIO(json.dumps({"data": {"user": self.contributions}}).encode())


class Fetch(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(g.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        self.contributions = user([1, 2] + [0] * 30)["contributionsCollection"]

    def fetch(self, api):
        with mock.patch.object(g.urllib.request, "urlopen", api):
            return g.fetch(LOGIN, "token-123")

    def test_one_request_for_the_contribution_calendar_only(self):
        api = FakeAPI({"contributionsCollection": self.contributions})
        u = self.fetch(api)
        self.assertEqual(g.summarise(u)["total"], 3)
        self.assertEqual(len(api.requests), 1)
        self.assertEqual(api.requests[0]["variables"], {"login": LOGIN})
        self.assertNotIn("repositor", api.requests[0]["query"])
        self.assertNotIn("token-123", json.dumps(api.requests))

    def test_unknown_user_is_rejected(self):
        with self.assertRaisesRegex(g.DataError, "missing user"):
            self.fetch(FakeAPI(None))

    def test_server_errors_are_retried(self):
        api = FakeAPI({"contributionsCollection": self.contributions})
        calls = []

        def flaky(req, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError(g.API, 502, "Bad Gateway", {}, None)
            return api(req, timeout)

        self.assertEqual(g.summarise(self.fetch(flaky))["total"], 3)
        self.assertEqual(len(calls), 2)
        self.sleep.assert_called_once_with(2)

    def test_bad_credentials_are_not_retried(self):
        calls = []

        def unauthorised(req, timeout):
            calls.append(1)
            raise urllib.error.HTTPError(g.API, 401, "Unauthorized", {}, None)

        with self.assertRaisesRegex(g.DataError, "HTTP 401") as caught:
            self.fetch(unauthorised)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("token-123", str(caught.exception))

    def test_network_failure_gives_up_after_three_attempts(self):
        calls = []

        def offline(req, timeout):
            calls.append(1)
            raise urllib.error.URLError("connection refused")

        with self.assertRaisesRegex(g.DataError, "request failed"):
            self.fetch(offline)
        self.assertEqual(len(calls), 3)

    def test_truncated_body_is_retried_then_rejected(self):
        with self.assertRaisesRegex(g.DataError, "request failed"):
            self.fetch(lambda req, timeout: io.BytesIO(b'{"data": {"us'))

    def test_graphql_errors_with_partial_data_are_rejected(self):
        body = {"data": {"user": {"contributionsCollection": None}}, "errors": [{"message": "Something went wrong"}]}
        with self.assertRaisesRegex(g.DataError, "Something went wrong"):
            self.fetch(lambda req, timeout: io.BytesIO(json.dumps(body).encode()))


class Main(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def run_from(self, u):
        src = self.dir / "response.json"
        src.write_text(json.dumps(u))
        with mock.patch("sys.stdout", io.StringIO()) as out:
            g.main(["--login", LOGIN, "--out", str(self.dir), "--from-json", str(src)])
        return out.getvalue(), {n: (self.dir / n).read_bytes() for n in ("activity.svg", "activity-mobile.svg")}

    def test_unchanged_data_regenerates_identical_files(self):
        u = user([0, 1, 2, 0, 3] * 70)
        _, first = self.run_from(u)
        _, second = self.run_from(u)
        self.assertEqual(first, second)

    def test_new_contribution_updates_the_card(self):
        counts = [0] * 366
        counts[-3:] = [2, 1, 0]
        _, before = self.run_from(user(counts))
        counts[-1] = 4  # a contribution lands today
        log, after = self.run_from(user(counts))
        self.assertNotEqual(before["activity.svg"], after["activity.svg"])
        self.assertNotEqual(before["activity-mobile.svg"], after["activity-mobile.svg"])
        self.assertIn("7 contributions in the past year on 3 days; current streak 3 days", log)
        self.assertIn(b"Last 30 days, today included: 7 contributions on 3 days.", after["activity.svg"])
        self.assertIn("last 30 days from 2026-09-27", log)

    def test_failed_fetch_leaves_existing_cards_untouched(self):
        for name in ("activity.svg", "activity-mobile.svg"):
            (self.dir / name).write_text("previous card")
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token-123"}), \
                mock.patch.object(g, "fetch", side_effect=g.DataError("GitHub API request failed: HTTP 502")):
            with self.assertRaises(SystemExit) as caught:
                g.main(["--login", LOGIN, "--out", str(self.dir)])
        self.assertIn("Activity card not updated", str(caught.exception))
        self.assertNotIn("token-123", str(caught.exception))
        self.assertEqual({(self.dir / n).read_text() for n in ("activity.svg", "activity-mobile.svg")},
                         {"previous card"})

    def test_short_calendar_leaves_existing_cards_untouched(self):
        (self.dir / "activity.svg").write_text("previous card")
        src = self.dir / "response.json"
        src.write_text(json.dumps(user([1] * 12)))
        with self.assertRaisesRegex(SystemExit, "fewer than 30"):
            g.main(["--login", LOGIN, "--out", str(self.dir), "--from-json", str(src)])
        self.assertEqual((self.dir / "activity.svg").read_text(), "previous card")

    def test_stale_live_response_leaves_existing_cards_untouched(self):
        (self.dir / "activity.svg").write_text("previous card")
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}), \
                mock.patch.object(g, "fetch", return_value=user([1] * 35, start=dt.date(2020, 1, 5))):
            with self.assertRaisesRegex(SystemExit, "ends 2020-02-08"):
                g.main(["--login", LOGIN, "--out", str(self.dir)])
        self.assertEqual((self.dir / "activity.svg").read_text(), "previous card")


class SVGOutput(unittest.TestCase):
    def test_valid_svg_with_stable_dimensions(self):
        for name, counts in (("zero", [0] * 367), ("sparse", [0, 1, 5, 0] * 92), ("dense", [40] * 367)):
            s = stats(counts, commits=9_999)
            desktop, mobile = g.render(s, GLYPHS), g.render(s, GLYPHS, mobile=True)
            with self.subTest(activity=name):
                g.validate(desktop)
                g.validate(mobile)
                self.assertIn('viewBox="0 0 1200 470" width="1200" height="470"', desktop)
                self.assertIn('viewBox="0 0 720 1044" width="720" height="1044"', mobile)

    def test_figures_stay_inside_their_columns(self):
        # Widest realistic values: the year row must end before the divider, the 30-day pair inside 1144.
        t = g.Type(GLYPHS)
        year = [g.figure_width(t, v, u, lab, sz, 13) for v, u, lab, sz in
                ((99_999, None, "CONTRIBUTIONS", 84), (366, None, "ACTIVE DAYS", 48),
                 (366, "DAYS", "LONGEST STREAK", 48), (9_999, None, "COMMITS", 48))]
        self.assertLess(56 + sum(year) + 3 * 36, 822)
        recent = [g.figure_width(t, v, None, lab, 48, 13) for v, lab in ((9_999, "CONTRIBUTIONS"), (30, "ACTIVE DAYS"))]
        self.assertLessEqual(854 + sum(recent) + 36, 1144)

    def test_no_external_resources(self):
        svg = g.render(stats([1] * 30), GLYPHS)
        self.assertNotIn("http://", svg.replace('xmlns="http://www.w3.org/2000/svg"', ""))
        self.assertNotIn("https://", svg)

    def test_validate_catches_broken_output(self):
        with self.assertRaisesRegex(g.DataError, "well-formed"):
            g.validate("<svg><g></svg>")
        with self.assertRaisesRegex(g.DataError, "undefined"):
            g.validate('<svg xmlns="http://www.w3.org/2000/svg"><use href="#nope"/></svg>')


if __name__ == "__main__":
    unittest.main()
