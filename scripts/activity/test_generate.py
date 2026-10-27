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


def repo(name, langs, private=False, fork=False, archived=False, mirror=False):
    return {"name": name, "isPrivate": private, "isFork": fork, "isArchived": archived, "isMirror": mirror,
            "languages": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                          "edges": [{"size": s, "node": {"name": n}} for n, s in langs.items()]}}


def user(counts, repos=(), start=dt.date(2025, 10, 26), commits=None, restricted=0):
    cal = calendar(start, counts)
    return {
        "contributionsCollection": {
            "startedAt": f"{start.isoformat()}T00:00:00Z",
            "endedAt": f"{(start + dt.timedelta(days=len(counts) - 1)).isoformat()}T23:59:59Z",
            "totalCommitContributions": sum(counts) if commits is None else commits,
            "restrictedContributionsCount": restricted,
            "contributionCalendar": cal,
        },
        "repositories": {"nodes": list(repos)},
    }


def stats(counts, repos=(), **kw):
    return g.summarise(user(counts, repos, **kw), LOGIN)


class Characterisation(unittest.TestCase):
    """Behaviour of the original generator that must survive the fixes."""

    def test_totals_active_days_and_longest_streak(self):
        counts = [0] * 367
        for i in (10, 11, 12, 40, 41, 300):
            counts[i] = 2
        s = g.summarise(user(counts, commits=9), LOGIN)
        self.assertEqual((s["total"], s["commits"], s["active_days"], s["longest_streak"]), (12, 9, 6, 3))

    def test_weeks_follow_github_calendar_weeks(self):
        counts = [1] * 10
        s = g.summarise(user(counts), LOGIN)
        self.assertEqual(s["weeks"], [(dt.date(2025, 10, 26), 7, 7), (dt.date(2025, 11, 2), 3, 3)])

    def test_language_shares_top_four_plus_other(self):
        repos = [repo("a", {"Python": 600, "CSS": 50}), repo("b", {"Java": 200, "Go": 100, "C": 30, "Lua": 20}),
                 repo(LOGIN, {"Python": 10_000})]
        s = g.summarise(user([0] * 7, repos), LOGIN)
        names = [n for n, _ in s["languages"]]
        self.assertEqual(names, ["Python", "Java", "Go", "CSS", "Other"])
        self.assertAlmostEqual(sum(v for _, v in s["languages"]), 1.0)
        self.assertAlmostEqual(dict(s["languages"])["Python"], 0.6)

    def test_render_keeps_card_dimensions(self):
        s = g.summarise(user([1] * 30, [repo("a", {"Java": 1})]), LOGIN)
        self.assertIn('viewBox="0 0 1200 470"', g.render(s, GLYPHS))
        self.assertIn('viewBox="0 0 720 964"', g.render(s, GLYPHS, mobile=True))

    def test_render_is_deterministic(self):
        s = g.summarise(user([0, 3, 1] * 100, [repo("a", {"Java": 5, "Python": 3})]), LOGIN)
        self.assertEqual(g.render(s, GLYPHS), g.render(s, GLYPHS))


class Streaks(unittest.TestCase):
    def streaks(self, counts, start=dt.date(2025, 10, 26)):
        s = stats(counts, start=start)
        return s["current_streak"], s["longest_streak"]

    def test_active_today_counts(self):
        self.assertEqual(self.streaks([0, 0, 1, 1]), (2, 2))

    def test_no_contribution_today_keeps_yesterdays_streak(self):
        self.assertEqual(self.streaks([0, 1, 1, 1, 0]), (3, 3))

    def test_no_contribution_today_or_yesterday_is_zero(self):
        self.assertEqual(self.streaks([1, 1, 1, 0, 0]), (0, 3))

    def test_across_midnight(self):
        # Contributions late on Mon Oct 27 and early on Tue Oct 28 fall on consecutive calendar days.
        week = [0] * 7
        week[1], week[2] = 3, 1  # calendar starts Sun Oct 26
        self.assertEqual(self.streaks(week[:3]), (2, 2))
        # The next run sees Wednesday, still empty: the streak survives until Wednesday is over ...
        self.assertEqual(self.streaks(week[:4]), (2, 2))
        # ... and is gone once Thursday is today.
        self.assertEqual(self.streaks(week[:5]), (0, 2))

    def test_broken_streak(self):
        self.assertEqual(self.streaks([1, 1, 1, 0, 1, 1]), (2, 3))
        self.assertEqual(self.streaks([1, 1, 0, 1, 1, 1, 1, 0, 1]), (1, 4))

    def test_year_boundary(self):
        # Sun Dec 28 2025 .. Fri Jan 2 2026
        s = stats([0, 0, 1, 2, 3, 1], start=dt.date(2025, 12, 28))
        self.assertEqual((s["current_streak"], s["longest_streak"], s["active_days"]), (4, 4, 4))
        self.assertEqual([w[0] for w in s["weeks"]], [dt.date(2025, 12, 28)])

    def test_leap_day(self):
        # Sun Feb 27 2028 .. Wed Mar 1 2028 passes through Feb 29.
        self.assertEqual(self.streaks([0, 1, 1, 1], start=dt.date(2028, 2, 27)), (3, 3))
        # Sun Dec 26 2027 .. Sat Dec 30 2028: every day of leap year 2028 is one unbroken run.
        s = stats([1] * 371, start=dt.date(2027, 12, 26))
        self.assertEqual(s["longest_streak"], 371)

    def test_non_leap_february_is_contiguous(self):
        # Sun Feb 28 2027 -> Mon Mar 1 2027
        self.assertEqual(self.streaks([1, 1], start=dt.date(2027, 2, 28)), (2, 2))

    def test_streak_reaching_window_start_is_marked_open(self):
        s = stats([1] * 10)
        self.assertTrue(s["current_streak_open"])
        self.assertIn("current streak at least 10 days", g.describe(s))
        s = stats([1] * 9 + [0])
        self.assertTrue(s["current_streak_open"])
        self.assertFalse(stats([0] + [1] * 9)["current_streak_open"])
        self.assertFalse(stats([0] * 10)["current_streak_open"])

    def test_empty_activity(self):
        s = stats([0] * 367)
        self.assertEqual((s["total"], s["active_days"], s["current_streak"], s["longest_streak"]), (0, 0, 0, 0))
        self.assertEqual(s["languages"], [])
        self.assertIn("current streak 0 days; longest streak 0 days", g.describe(s))
        for mobile in (False, True):
            g.validate(g.render(s, GLYPHS, mobile=mobile))


class CalendarValidation(unittest.TestCase):
    def test_duplicate_day_is_counted_once(self):
        u = user([1, 2, 3])
        days = u["contributionsCollection"]["contributionCalendar"]["weeks"][0]["contributionDays"]
        days.append(dict(days[1]))
        s = g.summarise(u, LOGIN)
        self.assertEqual((s["total"], s["active_days"], s["weeks"]), (6, 3, [(dt.date(2025, 10, 26), 3, 6)]))

    def test_conflicting_duplicate_day_is_rejected(self):
        u = user([1, 2, 3])
        days = u["contributionsCollection"]["contributionCalendar"]["weeks"][0]["contributionDays"]
        days.append({"date": days[1]["date"], "contributionCount": 7})
        with self.assertRaisesRegex(g.DataError, "conflicting"):
            g.summarise(u, LOGIN)

    def test_missing_days_are_rejected_not_bridged(self):
        u = user([1, 1, 0, 1, 1])
        del u["contributionsCollection"]["contributionCalendar"]["weeks"][0]["contributionDays"][2]
        with self.assertRaisesRegex(g.DataError, "gaps"):
            g.summarise(u, LOGIN)

    def test_missing_week_is_rejected(self):
        u = user([1] * 21)
        del u["contributionsCollection"]["contributionCalendar"]["weeks"][1]
        with self.assertRaisesRegex(g.DataError, "gaps"):
            g.summarise(u, LOGIN)

    def test_total_must_match_days(self):
        u = user([1, 2])
        u["contributionsCollection"]["contributionCalendar"]["totalContributions"] = 5
        with self.assertRaisesRegex(g.DataError, "total"):
            g.summarise(u, LOGIN)

    def test_empty_calendar_is_rejected(self):
        u = user([0])
        u["contributionsCollection"]["contributionCalendar"]["weeks"] = []
        with self.assertRaisesRegex(g.DataError, "empty"):
            g.summarise(u, LOGIN)

    def test_partial_response_is_rejected(self):
        for path in (("contributionsCollection",), ("contributionsCollection", "contributionCalendar"),
                     ("contributionsCollection", "totalCommitContributions"), ("repositories",)):
            u = user([1, 2])
            parent = u
            for key in path[:-1]:
                parent = parent[key]
            parent[path[-1]] = None
            with self.subTest(path=path), self.assertRaisesRegex(g.DataError, "missing"):
                g.summarise(u, LOGIN)

    def test_negative_count_is_rejected(self):
        u = user([1, 2])
        u["contributionsCollection"]["contributionCalendar"]["weeks"][0]["contributionDays"][0]["contributionCount"] = -1
        u["contributionsCollection"]["contributionCalendar"]["totalContributions"] = 1
        with self.assertRaisesRegex(g.DataError, "invalid"):
            g.summarise(u, LOGIN)

    def test_stale_calendar_is_rejected(self):
        s = stats([1] * 10)  # ends Tue Nov 4 2025
        g.check_fresh(s, dt.date(2025, 11, 4))
        g.check_fresh(s, dt.date(2025, 11, 5))  # runner already past midnight UTC
        g.check_fresh(s, dt.date(2025, 11, 3))  # token dated ahead of UTC
        with self.assertRaisesRegex(g.DataError, "ends 2025-11-04"):
            g.check_fresh(s, dt.date(2025, 11, 7))


class Languages(unittest.TestCase):
    def test_bytes_are_summed_across_repositories(self):
        s = stats([0], [repo("a", {"Python": 300, "Java": 100}), repo("b", {"Python": 100, "Java": 500})])
        self.assertEqual(s["languages"], [("Java", 0.6), ("Python", 0.4)])
        self.assertEqual(s["repos_counted"], 2)

    def test_ties_are_ordered_by_name(self):
        s = stats([0], [repo("a", {"Rust": 1, "Go": 1, "C": 1})])
        self.assertEqual([n for n, _ in s["languages"]], ["C", "Go", "Rust"])

    def test_forks_archives_mirrors_and_profile_repo_are_excluded(self):
        repos = [repo("kept", {"Java": 10}), repo("f", {"Go": 99}, fork=True),
                 repo("old", {"Perl": 99}, archived=True), repo("m", {"C": 99}, mirror=True),
                 repo(LOGIN.upper(), {"Python": 99})]
        s = stats([0], repos)
        self.assertEqual(s["languages"], [("Java", 1.0)])
        self.assertEqual(s["repos_skipped"], {"fork": 1, "archived": 1, "mirror": 1, "profile": 1})

    def test_private_repositories_never_reach_the_card(self):
        repos = [repo("public-app", {"Java": 10}), repo("secret-project", {"Dart": 1_000_000}, private=True)]
        s = stats([0], repos)
        self.assertEqual(s["languages"], [("Java", 1.0)])
        self.assertEqual(s["repos_skipped"], {"private": 1})
        for mobile in (False, True):
            svg = g.render(s, GLYPHS, mobile=mobile)
            self.assertNotIn("Dart", svg)
            self.assertNotIn("secret", svg.lower())

    def test_missing_visibility_fails_closed(self):
        r = repo("a", {"Java": 1})
        del r["isPrivate"]
        with self.assertRaisesRegex(g.DataError, "isPrivate"):
            stats([0], [r])

    def test_repository_listed_twice_is_counted_once(self):
        s = stats([0], [repo("a", {"Java": 10}), repo("b", {"Go": 10}), repo("A", {"Java": 10})])
        self.assertEqual(s["languages"], [("Go", 0.5), ("Java", 0.5)])

    def test_duplicate_language_edge_is_counted_once(self):
        r = repo("a", {"Java": 10, "Go": 10})
        r["languages"]["edges"].append({"size": 10, "node": {"name": "Java"}})
        self.assertEqual(stats([0], [r])["languages"], [("Go", 0.5), ("Java", 0.5)])
        r["languages"]["edges"].append({"size": 11, "node": {"name": "Java"}})
        with self.assertRaisesRegex(g.DataError, "conflicting"):
            stats([0], [r])

    def test_no_public_code(self):
        s = stats([0], [repo("empty", {})])
        self.assertEqual(s["languages"], [])
        self.assertIn("Languages by code size in public repositories: none yet.", g.describe(s))

    def test_private_contribution_counts_are_labelled(self):
        s = stats([2, 3], restricted=4)
        self.assertIn("5 contributions (including 4 private, as counts only)", g.describe(s))


class FakeAPI:
    """Stands in for urllib.request.urlopen, answering by query type and cursor."""

    def __init__(self, contributions, repo_pages, language_pages=None):
        self.contributions, self.repo_pages = contributions, repo_pages
        self.language_pages = language_pages or {}
        self.requests = []

    def __call__(self, req, timeout):
        body = json.loads(req.data)
        self.requests.append(body)
        query, after = body["query"], body["variables"].get("after")
        if "contributionsCollection" in query:
            data = {"user": self.contributions}
        elif "repository(" in query:
            data = {"repository": {"languages": self.language_pages[body["variables"]["name"]][after]}}
        else:
            data = {"user": {"repositories": self.repo_pages[after]}}
        return io.BytesIO(json.dumps({"data": data}).encode())


def page(nodes, next_cursor=None, key="nodes"):
    return {"pageInfo": {"hasNextPage": next_cursor is not None, "endCursor": next_cursor}, key: nodes}


class Fetch(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(g.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        self.contributions = user([1, 2])["contributionsCollection"]

    def fetch(self, api):
        with mock.patch.object(g.urllib.request, "urlopen", api):
            return g.fetch(LOGIN, "token-123")

    def test_paginates_repositories_and_languages(self):
        big = repo("big", {"Python": 50})
        big["languages"]["pageInfo"] = {"hasNextPage": True, "endCursor": "L1"}
        api = FakeAPI(
            {"contributionsCollection": self.contributions},
            {None: page([repo("a", {"Java": 10}), big], "R1"), "R1": page([repo("c", {"Go": 40})])},
            {"big": {None: page([{"size": 50, "node": {"name": "Python"}}], "L1", key="edges"),
                     "L1": page([{"size": 100, "node": {"name": "Zig"}}], key="edges")}},
        )
        u = self.fetch(api)
        self.assertEqual([r["name"] for r in u["repositories"]["nodes"]], ["a", "big", "c"])
        s = g.summarise(u, LOGIN)
        self.assertEqual(dict(s["languages"]), {"Zig": 0.5, "Python": 0.25, "Go": 0.2, "Java": 0.05})
        self.assertEqual(len(api.requests), 5)  # contributions, 2 repo pages, 2 language pages
        self.assertTrue(all("token-123" not in json.dumps(r) for r in api.requests))

    def test_cursor_that_does_not_advance_is_rejected(self):
        api = FakeAPI({"contributionsCollection": self.contributions}, {None: page([], "R1"), "R1": page([], "R1")})
        with self.assertRaisesRegex(g.DataError, "cursor"):
            self.fetch(api)

    def test_null_repository_node_is_rejected(self):
        api = FakeAPI({"contributionsCollection": self.contributions}, {None: page([None])})
        with self.assertRaisesRegex(g.DataError, "missing"):
            self.fetch(api)

    def test_unknown_user_is_rejected(self):
        api = FakeAPI(None, {})
        with self.assertRaisesRegex(g.DataError, "missing user"):
            self.fetch(api)

    def test_server_errors_are_retried(self):
        api = FakeAPI({"contributionsCollection": self.contributions}, {None: page([])})
        calls = []

        def flaky(req, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError(g.API, 502, "Bad Gateway", {}, None)
            return api(req, timeout)

        self.assertEqual(self.fetch(flaky)["repositories"]["nodes"], [])
        self.assertEqual(len(calls), 3)
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
        u = user([0, 1, 2, 0, 3] * 70, [repo("a", {"Java": 7, "C": 3})])
        _, first = self.run_from(u)
        _, second = self.run_from(u)
        self.assertEqual(first, second)

    def test_new_contribution_updates_the_card(self):
        counts = [0] * 366
        counts[-3:] = [2, 1, 0]
        _, before = self.run_from(user(counts, [repo("a", {"Java": 1})]))
        counts[-1] = 4  # a contribution lands today
        log, after = self.run_from(user(counts, [repo("a", {"Java": 1})]))
        self.assertNotEqual(before["activity.svg"], after["activity.svg"])
        self.assertNotEqual(before["activity-mobile.svg"], after["activity-mobile.svg"])
        self.assertIn("7 contributions in the past year on 3 days; current streak 3 days", log)
        self.assertIn(b"current streak 3 days", after["activity.svg"])

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

    def test_stale_live_response_leaves_existing_cards_untouched(self):
        (self.dir / "activity.svg").write_text("previous card")
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}), \
                mock.patch.object(g, "fetch", return_value=user([1] * 7, start=dt.date(2020, 1, 5))):
            with self.assertRaisesRegex(SystemExit, "ends 2020-01-11"):
                g.main(["--login", LOGIN, "--out", str(self.dir)])
        self.assertEqual((self.dir / "activity.svg").read_text(), "previous card")


class SVGOutput(unittest.TestCase):
    def test_valid_svg_with_stable_dimensions(self):
        for n_langs in (0, 1, 4, 7):
            langs = {f"L{i}": 10 + i for i in range(n_langs)}
            s = stats([0, 1, 5, 0] * 92, [repo("a", langs)] if langs else [])
            rows = len(s["languages"])
            desktop, mobile = g.render(s, GLYPHS), g.render(s, GLYPHS, mobile=True)
            with self.subTest(languages=n_langs):
                g.validate(desktop)
                g.validate(mobile)
                self.assertIn('viewBox="0 0 1200 470" width="1200" height="470"', desktop)
                height = 892 + max(rows - 1, 0) * 48 + 72
                self.assertIn(f'viewBox="0 0 720 {height}" width="720" height="{height}"', mobile)

    def test_no_external_resources(self):
        svg = g.render(stats([1] * 30, [repo("a", {"Java": 1})]), GLYPHS)
        self.assertNotIn("http://", svg.replace('xmlns="http://www.w3.org/2000/svg"', ""))
        self.assertNotIn("https://", svg)

    def test_validate_catches_broken_output(self):
        with self.assertRaisesRegex(g.DataError, "well-formed"):
            g.validate("<svg><g></svg>")
        with self.assertRaisesRegex(g.DataError, "undefined"):
            g.validate('<svg xmlns="http://www.w3.org/2000/svg"><use href="#nope"/></svg>')


if __name__ == "__main__":
    unittest.main()
