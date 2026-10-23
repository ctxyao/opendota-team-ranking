"""Deterministic offline tests (Python 3.9+): python3 -m unittest -v

Every data-handling rule in README's policy table has a test named after it.
The optional ParserCrossCheckTests run only when PyYAML happens to be installed;
the solution itself never imports it.
"""

import io
import json
import os
import random
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch

import template as app

try:
    import yaml  # Optional cross-check only; not a dependency.
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parent
AS_OF = datetime(2026, 1, 3, tzinfo=timezone.utc)
DAY = 86400 * 10**6  # One day in microseconds, the output unit.


def player(account_id=1, team_id=10, history="2026-01-01T00:00:00.000Z", **extra):
    row = dict(account_id=account_id, team_id=team_id, full_history_time=history,
               personaname="Player", country_code="us")
    row.update(extra)
    return row


def select(rows, n=5):
    return app.select_teams(rows, n, AS_OF)


class Response(io.BytesIO):
    """Minimal urlopen() result. Like http.client, read() raises IncompleteRead when the
    body is shorter than Content-Length (checked against real sockets on 3.9-3.15)."""

    def __init__(self, body, length=None):
        super().__init__(body)
        self.declared = len(body) if length is None else length
        self.headers = {"Content-Length": str(self.declared)}

    def read(self, *args):
        data = super().read(*args)
        if not args and len(data) < self.declared:  # read(n) returns short data silently.
            raise IncompleteRead(data, self.declared - len(data))
        return data


class RankingTests(unittest.TestCase):
    def test_rank_by_summed_experience_not_team_id_or_roster_size(self):
        teams = select([player(1, 10), player(2, 10), player(3, 20, "2025-12-29T00:00:00Z")], 1)
        self.assertEqual([t["team_id"] for t in teams], [20])
        self.assertEqual(teams[0]["team_experience"], 5 * DAY)

    def test_experience_is_exact_microseconds_and_total_equals_sum(self):
        team = select([player(1, history="2026-01-02T23:59:59.999Z"),
                       player(2, history="2026-01-02T23:59:58.001Z")])[0]
        self.assertEqual([p["player_experience"] for p in team["players"]], [1999000, 1000])
        self.assertEqual(team["team_experience"], 2000000)

    def test_ranking_uses_exact_elapsed_time_not_rounded_seconds(self):
        # Rounding each player down to whole seconds would rank team 20 (1.1 s + 0.1 s -> 1 s)
        # above team 10 (0.9 s + 0.9 s -> 0 s); the defined sums are 1.2 s and 1.8 s.
        rows = [player(1, 10, "2026-01-02T23:59:59.100Z"), player(2, 10, "2026-01-02T23:59:59.100Z"),
                player(3, 20, "2026-01-02T23:59:58.900Z"), player(4, 20, "2026-01-02T23:59:59.900Z")]
        self.assertEqual([(t["team_id"], t["team_experience"]) for t in select(rows, 1)], [(10, 1800000)])

    def test_timestamp_formats_identical_on_every_python_version(self):
        whole = ["2026-01-02T00:00:00Z", "2026-01-02T00:00:00.000000Z",
                 "2026-01-02T01:00:00+01:00", "2026-01-02T00:00:00.000+0000"]
        fractional = {"2026-01-02T00:00:00.1Z": 100000, "2026-01-02T00:00:00.12Z": 120000,
                      "2026-01-02T00:00:00.1234Z": 123400}
        for value, expected in [(v, DAY) for v in whole] + [(v, DAY - f) for v, f in fractional.items()]:
            with self.subTest(value=value):  # Fractions are kept exactly, to the microsecond.
                self.assertEqual(select([player(history=value)])[0]["team_experience"], expected)

    def test_unusable_history_excludes_player_but_keeps_teammates(self):
        invalid = [None, "", "bad", "2026-01-01", "2026-01-01T00:00:00", "2026-01-04T00:00:00Z",
                   "2026-01-01 00:00:00Z", 1767225600, True, "2010-12-31T23:59:59Z",
                   "0001-01-01T00:00:00+23:59"]  # Dota 2 did not exist before 2011.
        rows = [player()] + [player(i + 2, history=value) for i, value in enumerate(invalid)]
        team = select(rows)[0]
        self.assertEqual((len(team["players"]), team["team_experience"]), (1, 2 * DAY))

    def test_zero_experience_is_valid(self):
        self.assertEqual(select([player(history="2026-01-03T00:00:00Z")])[0]["team_experience"], 0)

    def test_teamless_and_malformed_rows_are_not_ranked(self):
        rows = [player(i + 1, tid) for i, tid in enumerate([None, 0, -1, True, "10", 1.5])]
        rows += [None, [], "bad", player(99, 10)]
        self.assertEqual([t["team_id"] for t in select(rows)], [10])

    def test_no_eligible_team_fails_closed(self):
        for rows in ([], [player(team_id="10")], [player(history=None)]):
            with self.subTest(rows=rows), self.assertRaises(app.ApiError):
                select(rows)

    def test_wrong_top_level_shape_fails(self):
        with self.assertRaises(app.ApiError):
            select({"error": "not a list"})

    def test_repeated_account_counted_once(self):
        team = select([player(), player(), player(2)])[0]
        self.assertEqual(len(team["players"]), 2)

    def test_invalid_account_ids_neither_crash_nor_hide_valid_players(self):
        for bad in ([], {}, True, 1.0, "1", None):  # [] is unhashable; True == 1.0 == 1 in a set.
            with self.subTest(bad=bad):
                teams = select([player(bad, 20, "2026-01-02T00:00:00Z"), player(1, 10)])
                self.assertEqual([(t["team_id"], t["team_experience"]) for t in teams],
                                 [(10, 2 * DAY), (20, DAY)])

    def test_conflicting_repeats_exclude_the_player_in_any_order(self):
        rows = [player(7, 10, team_name="Ghost"), player(7, 20),  # one account on two teams
                player(8, 10), player(8, 10, "2025-12-31T00:00:00Z"),  # one account, two histories
                player(9, 30), player(10, 40, "2026-01-02T00:00:00Z")]
        for order in (rows, rows[::-1]):
            self.assertEqual([t["team_id"] for t in select(order)], [30, 40])
        kept = select([player(7, 10, team_name="Ghost"), player(7, 20), player(9, 10)])
        self.assertEqual([(t["team_id"], t["team_name"]) for t in kept], [(10, None)])  # No name from excluded rows.

    def test_malformed_team_ids_warn_but_teamless_and_identical_rows_only_inform(self):
        rows = [player(1, 10), player(1, 10), player(2, 0), player(3, None), player(4, "10"), player(5, 1.5)]
        with patch.object(app.LOG, "disabled", False), self.assertLogs(app.LOG, "INFO") as logs:
            select(rows, 1)
        warnings = [r.getMessage() for r in logs.records if r.levelname == "WARNING"]
        self.assertEqual(warnings, ["Skipped 2 malformed records or team_id values"])

    def test_ineligible_duplicate_does_not_discard_an_eligible_player(self):
        for unusable in (player(7, 20, None), player(7, 0), player(7, "20")):
            with self.subTest(unusable=unusable):
                good = player(7, 10)
                for rows in ([good, unusable], [unusable, good]):
                    self.assertEqual([(t["team_id"], t["team_experience"]) for t in select(rows, 1)],
                                     [(10, 2 * DAY)])

    def test_eligible_players_returns_countable_rows_and_reasons(self):
        rows = [player(1, 10), player(1, 10), player(2, 0), player(3, "10"), player(4, 10, None),
                player(5, 10), player(5, 20), player(6, 20)]
        records, counts = app.eligible_players(rows, AS_OF)
        self.assertEqual([(team_id, row["account_id"]) for team_id, _, row in records], [(10, 1), (20, 6)])
        self.assertEqual(counts, {"teamless": 1, "malformed": 1, "unusable": 1, "repeats": 2, "conflicts": 1})

    def test_missing_account_ids_are_not_merged(self):
        a, b = player(), player(2)
        del a["account_id"], b["account_id"]
        self.assertEqual(len(select([a, b])[0]["players"]), 2)

    def test_fixed_schema_with_null_for_missing_display_fields(self):
        row = player()
        del row["personaname"]
        row["country_code"] = "\ud800"  # Lone surrogate: strict YAML would reject it.
        team = select([row])[0]
        self.assertEqual(list(team), list(app.TEAM_FIELDS) + ["players"])
        self.assertEqual(team["players"], [{"personaname": None, "player_experience": 2 * DAY,
                                            "country_code": None}])
        self.assertEqual((team["wins"], team["losses"], team["rating"]), (None, None, None))

    def test_snapshot_team_name_used_only_when_unambiguous(self):
        self.assertEqual(select([player(team_name="A"), player(2, team_name="A")])[0]["team_name"], "A")
        self.assertIsNone(select([player(team_name="A"), player(2, team_name="B")])[0]["team_name"])

    def test_ties_and_player_order_are_deterministic(self):
        a, b = player(1, 20), player(2, 10)
        self.assertEqual(select([a, b]), select([b, a]))
        self.assertEqual([t["team_id"] for t in select([a, b])], [10, 20])

    def test_fewer_eligible_teams_than_requested_is_success(self):
        self.assertEqual(len(select([player()], 100)), 1)

    def test_seeded_malformed_samples_keep_valid_data_and_stable_ranking(self):
        rng = random.Random(2026)
        junk = [None, True, 0, -1, 1.0, 1.5, "", "x", "10", "\ud800", [], {}, [1], 10**30,
                "2026-01-01", "2027-01-01T00:00:00Z"]
        histories = ["2026-01-01T00:00:00Z", "2026-01-01T12:00:00.250+0000", "2026-01-02T23:59:59.900Z"]
        for _ in range(300):
            rows = []
            for _ in range(rng.randint(1, 12)):
                row = {"account_id": rng.choice([1, 2, 3, 4, rng.choice(junk)]),
                       "team_id": rng.choice([1, 2, 3, rng.choice(junk)]),
                       "full_history_time": rng.choice(histories + [rng.choice(junk)]),
                       "personaname": rng.choice(["p", rng.choice(junk)]),
                       "country_code": rng.choice(["us", rng.choice(junk)])}
                rows.append(rng.choice(junk) if rng.random() < 0.05 else row)
            # A unique valid row makes rejection a failure, not an untested skipped case.
            rows.append(player(9999, 99))
            teams = select(rows, 3)
            shuffled = rows[:]
            rng.shuffle(shuffled)
            summary = [(t["team_id"], t["team_experience"]) for t in teams]
            self.assertEqual([(t["team_id"], t["team_experience"]) for t in select(shuffled, 3)], summary)
            self.assertEqual(summary, sorted(summary, key=lambda s: (-s[1], s[0])))
            for team in teams:
                self.assertEqual(team["team_experience"], sum(p["player_experience"] for p in team["players"]))
            app.render_yaml({"as_of": "x", "experience_unit": "microseconds", "teams": teams})


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.opener = self.start(patch.object(app, "urlopen"))
        self.sleep = Mock()  # Injected, so the real time module is never patched.
        self.client = app.ApiClient(sleep=self.sleep, clock=lambda: 100.0)

    def start(self, patcher):
        """Python 3.9-compatible stand-in for TestCase.enterContext (3.11+)."""
        self.addCleanup(patcher.stop)
        return patcher.start()

    def http_error(self, code, headers=None):
        return HTTPError("http://test/api", code, "test", headers or {}, io.BytesIO())

    def test_request_headers_timeout_and_pacing(self):
        self.opener.side_effect = [Response(b"[]"), Response(b"{}")]
        self.assertEqual(self.client.get("/proPlayers"), [])
        self.assertEqual(self.client.get("/teams/10"), {})
        request = self.opener.call_args_list[0][0][0]
        self.assertEqual(request.full_url, "https://api.opendota.com/api/proPlayers")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(self.opener.call_args[1]["timeout"], app.REQUEST_TIMEOUT)
        self.sleep.assert_called_with(app.REQUEST_INTERVAL)

    def test_injected_endpoint_and_pace_are_used(self):
        client = app.ApiClient("http://127.0.0.1:9", interval=0.25, sleep=self.sleep, clock=lambda: 100.0)
        self.opener.side_effect = [Response(b"[]"), Response(b"[]")]
        client.get("/proPlayers")
        client.get("/proPlayers")
        self.assertEqual(self.opener.call_args[0][0].full_url, "http://127.0.0.1:9/proPlayers")
        self.assertEqual([c[0][0] for c in self.sleep.call_args_list], [0.0, 0.25])

    def test_retry_after_seconds_reads_both_header_forms(self):
        now = datetime(2026, 1, 3, 0, 0, 0, 500000, tzinfo=timezone.utc)
        in_30 = format_datetime(datetime(2026, 1, 3, 0, 0, 30, tzinfo=timezone.utc), usegmt=True)
        for value, expected in [("7", 7), (" 7 ", 7), ("3600", 3600), (in_30, 30),  # 29.5 s rounds up
                                ("Thu, 01 Jan 1970 00:00:00 GMT", 0), ("", None), (None, None),
                                ("soon", None), ("-5", None), ("1.5", None)]:
            with self.subTest(value=value):
                self.assertEqual(app.retry_after_seconds(value, now), expected)

    def test_transient_failures_retry_three_times_and_name_the_cause(self):
        for error, cause in [(URLError(ConnectionRefusedError(61, "Connection refused")), "refused"),
                             (TimeoutError("timed out"), "timed out"),
                             (self.http_error(503), "HTTP 503")]:
            with self.subTest(cause=cause):
                self.opener.reset_mock()
                self.opener.side_effect = error
                with self.assertRaisesRegex(app.ApiError, cause):
                    self.client.get("/proPlayers")
                self.assertEqual(self.opener.call_count, 3)

    def test_http_status_survives_each_failure_exit(self):
        for code, headers, attempts in ((403, {}, 1), (429, {}, 3),
                                        (429, {"Retry-After": "61"}, 1), (503, {}, 3)):
            with self.subTest(code=code, headers=headers):
                self.opener.reset_mock()
                self.opener.side_effect = self.http_error(code, headers)
                with self.assertRaises(app.ApiError) as caught:
                    self.client.get("/teams/10")
                self.assertEqual(getattr(caught.exception, "status", None), code)
                self.assertEqual(self.opener.call_count, attempts)

    def test_network_failure_does_not_inherit_a_previous_http_status(self):
        self.opener.side_effect = [self.http_error(429), URLError("offline"), URLError("offline")]
        with self.assertRaises(app.ApiError) as caught:
            self.client.get("/teams/10")
        self.assertIsNone(getattr(caught.exception, "status", None))
        self.assertIn("offline", str(caught.exception))

    def test_certificate_failure_is_not_retried(self):
        reason = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] verify failed")
        self.opener.side_effect = URLError(reason)
        with self.assertRaisesRegex(app.ApiError, "CERTIFICATE_VERIFY_FAILED"):
            self.client.get("/proPlayers")
        self.assertEqual(self.opener.call_count, 1)

    def test_body_shorter_than_content_length_is_retried(self):
        self.opener.side_effect = [Response(b"[1", 100), Response(b"[1]")]
        self.assertEqual(self.client.get("/proPlayers"), [1])
        self.assertEqual(self.opener.call_count, 2)

    def test_retry_after_seconds_honoured_and_long_waits_refused(self):
        self.opener.side_effect = [self.http_error(429, {"Retry-After": "7"}), Response(b"[]")]
        self.assertEqual(self.client.get("/proPlayers"), [])
        self.sleep.assert_any_call(7)
        self.opener.reset_mock()
        self.opener.side_effect = self.http_error(429, {"Retry-After": "3600"})
        with self.assertRaisesRegex(app.ApiError, "Retry-After"):
            self.client.get("/proPlayers")
        self.assertEqual(self.opener.call_count, 1)

    def test_retry_after_http_date_is_honoured_too(self):
        soon = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
        self.opener.side_effect = [self.http_error(429, {"Retry-After": soon}), Response(b"[]")]
        self.assertEqual(self.client.get("/proPlayers"), [])
        self.assertIn(self.sleep.call_args_list[1][0][0], (29, 30))  # [0] is the pacing sleep.
        self.opener.reset_mock()
        later = format_datetime(datetime.now(timezone.utc) + timedelta(hours=1), usegmt=True)
        self.opener.side_effect = self.http_error(429, {"Retry-After": later})
        with self.assertRaisesRegex(app.ApiError, "Retry-After"):
            self.client.get("/proPlayers")
        self.assertEqual(self.opener.call_count, 1)

    def test_permanent_http_errors_are_not_retried(self):
        for code in (400, 401, 403, 404):
            with self.subTest(code=code):
                self.opener.reset_mock()
                self.opener.side_effect = self.http_error(code)
                with self.assertRaisesRegex(app.ApiError, "HTTP %d" % code):
                    self.client.get("/teams/10")
                self.assertEqual(self.opener.call_count, 1)

    def test_empty_and_invalid_bodies_fail_without_retry(self):
        for body, cause in [(b"", "empty response"), (b" \r\n", "empty response"),
                            (b"<html>", "invalid JSON"), (b"\xff", "invalid JSON"), (b"[" * 2000, "invalid JSON")]:
            with self.subTest(body=body[:8]):
                self.opener.reset_mock()
                self.opener.side_effect = [Response(body)]
                with self.assertRaisesRegex(app.ApiError, cause):
                    self.client.get("/proPlayers")


class ReportTests(unittest.TestCase):
    def client(self, *responses):
        client = Mock()
        client.get.side_effect = list(responses)
        return client

    def test_only_selected_teams_are_fetched_and_metadata_attached(self):
        client = self.client([player(1, 10), player(2, 20, "2026-01-02T00:00:00Z")],
                             {"name": "Team", "wins": 0, "losses": 4, "rating": 1234.5})
        report = app.build_report(client, 1, AS_OF)
        self.assertEqual([c[0][0] for c in client.get.call_args_list], ["/proPlayers", "/teams/10"])
        self.assertEqual((report["as_of"], report["experience_unit"]), ("2026-01-03T00:00:00Z", "microseconds"))
        team = report["teams"][0]
        self.assertEqual((team["team_name"], team["wins"], team["losses"], team["rating"]),
                         ("Team", 0, 4, 1234.5))

    def test_matching_or_missing_detail_id_keeps_available_metadata(self):
        for identity in ({}, {"team_id": None}, {"team_id": 10}):
            with self.subTest(identity=identity):
                details = dict(identity, name="Verified", wins=3, losses=2, rating=10.5)
                team = app.build_report(self.client([player()], details), 1, AS_OF)["teams"][0]
                self.assertEqual((team["team_name"], team["wins"], team["rating"]),
                                 ("Verified", 3, 10.5))

    def test_conflicting_or_invalid_detail_id_rejects_metadata_not_ranking(self):
        for wrong in (20, 0, -1, True, 10.0, "10", [], {}):
            with self.subTest(identity=wrong):
                rows = [player(1, 10, team_name="Snapshot"), player(2, 20, "2026-01-02T00:00:00Z")]
                client = self.client(rows, {"team_id": wrong, "name": "Wrong", "wins": 99,
                                           "losses": 1, "rating": 999},
                                     {"team_id": 20, "name": "Second", "wins": 2})
                with patch.object(app.LOG, "disabled", False), patch.object(app.LOG, "warning") as warning:
                    teams = app.build_report(client, 2, AS_OF)["teams"]
                self.assertEqual([(t["team_id"], t["team_experience"]) for t in teams],
                                 [(10, 2 * DAY), (20, DAY)])
                self.assertEqual([teams[0][f] for f in ("team_name", "wins", "losses", "rating")],
                                 ["Snapshot", None, None, None])
                self.assertEqual((teams[1]["team_name"], teams[1]["wins"]), ("Second", 2))
                self.assertEqual(client.get.call_count, 3)
                warning.assert_called_once()

    def test_team_detail_failure_nulls_only_that_team(self):
        rows = [player(1, 10), player(2, 20, "2026-01-02T00:00:00Z", team_name="Snap")]
        for failure in (app.ApiError("/teams/10: HTTP 503 (after 3 attempts)"),
                        app.ApiError("/teams/10: empty response"), [], None):
            with self.subTest(failure=failure):
                client = self.client(rows, failure, {"name": "Second", "wins": 3})
                teams = app.build_report(client, 2, AS_OF)["teams"]
                self.assertEqual([t["team_id"] for t in teams], [10, 20])
                self.assertEqual((teams[0]["wins"], teams[0]["rating"]), (None, None))
                self.assertEqual((teams[1]["team_name"], teams[1]["wins"]), ("Second", 3))

    def test_rate_limit_stops_remaining_detail_requests(self):
        rows = [player(1, 10), player(2, 20, "2026-01-02T00:00:00Z"), player(3, 30, "2026-01-02T12:00:00Z")]
        client = self.client(rows, app.ApiError("/teams/10: HTTP 429 (after 3 attempts)", status=429))
        teams = app.build_report(client, 3, AS_OF)["teams"]
        self.assertEqual(client.get.call_count, 2)  # /proPlayers and the first team only.
        self.assertEqual([(t["team_id"], t["wins"]) for t in teams], [(10, None), (20, None), (30, None)])

    def test_rate_limit_control_uses_status_not_error_wording(self):
        rows = [player(1, 10), player(2, 20, "2026-01-02T00:00:00Z")]
        for status, text in ((429, "quota exhausted"), (503, "upstream note mentions HTTP 429"),
                             (None, "diagnostic mentions HTTP 429")):
            with self.subTest(status=status):
                failure = app.ApiError(text)
                failure.status = status  # Independent of ApiError's constructor interface.
                client = self.client(rows, failure, {"name": "Second", "wins": 2})
                teams = app.build_report(client, 2, AS_OF)["teams"]
                limited = status == 429
                self.assertEqual(client.get.call_count, 2 if limited else 3)
                self.assertEqual(teams[1]["wins"], None if limited else 2)

    def test_negative_finite_rating_is_preserved_but_negative_counts_are_not(self):
        for rating in (-1, -10.5):
            with self.subTest(rating=rating):
                details = {"wins": -1, "losses": -2, "rating": rating}
                team = app.build_report(self.client([player()], details), 1, AS_OF)["teams"][0]
                self.assertEqual((team["wins"], team["losses"], team["rating"]), (None, None, rating))

    def test_invalid_metadata_values_stay_null(self):
        for details in ({"name": "", "wins": True, "losses": -1, "rating": float("nan")},
                        {"name": "\ud800", "wins": "0", "losses": 1.5, "rating": float("inf")}):
            with self.subTest(details=details):
                team = app.build_report(self.client([player(team_name="Snap")], details), 1, AS_OF)["teams"][0]
                self.assertEqual([team[k] for k in ("team_name", "wins", "losses", "rating")],
                                 ["Snap", None, None, None])

    def test_primary_fetch_failure_propagates(self):
        with self.assertRaises(app.ApiError):
            app.build_report(self.client(app.ApiError("/proPlayers: HTTP 503")), 1, AS_OF)


class OutputTests(unittest.TestCase):
    def report(self, **team_details):
        details = {"name": "Team", "wins": 0, "losses": 1, "rating": 12.5}
        details.update(team_details)
        client = Mock()
        client.get.side_effect = [[player()], details]
        return app.build_report(client, 1, AS_OF)

    def test_fixed_schema_golden_yaml(self):
        self.assertEqual(app.render_yaml(self.report(wins=None, rating=None)), '''as_of: "2026-01-03T00:00:00Z"
experience_unit: "microseconds"
teams:
  - team_name: "Team"
    team_id: 10
    wins: null
    losses: 1
    rating: null
    team_experience: 172800000000
    players:
      - personaname: "Player"
        player_experience: 172800000000
        country_code: "us"
''')

    def test_strings_are_quoted_and_yaml_line_breaks_escaped(self):
        name = 'yes: # \\"\n\t\x00\x7f\x85\u2028\u2029\ufffe\uffff 中文 🎮'
        text = app.render_yaml(self.report(name=name))
        line = next(line for line in text.splitlines() if "team_name:" in line)
        self.assertEqual(json.loads(line.split(": ", 1)[1]), name)
        for char in "\x85\u2028\u2029\ufffe\uffff":
            self.assertNotIn(char, text)
        self.assertIn("🎮", text)  # Literal, not a JSON surrogate pair.
        self.assertIn('team_name: "null"', app.render_yaml(self.report(name="null")))

    def test_lone_surrogate_scalar_rejected(self):
        with self.assertRaises(UnicodeError):
            app.yaml_scalar("\ud800")

    def test_scientific_float_keeps_a_decimal_point(self):
        self.assertEqual(app.yaml_scalar(1e-08), "1.0e-08")

    def test_stdout_is_utf8_regardless_of_locale(self):
        raw = io.BytesIO()
        fake = io.TextIOWrapper(raw, encoding="latin-1")
        with patch.object(app.sys, "stdout", fake):
            app.write_output("中文 🎮\n", "-")
        self.assertEqual(raw.getvalue(), "中文 🎮\n".encode("utf-8"))

    def test_file_write_keeps_symlink_hardlink_and_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            real, link, alias = folder / "real.yaml", folder / "link.yaml", folder / "alias.yaml"
            real.write_text("old")
            real.chmod(0o644)
            link.symlink_to(real)
            os.link(real, alias)
            app.write_output("new\n", str(link))
            self.assertTrue(link.is_symlink())
            self.assertEqual((real.read_text(), alias.read_text()), ("new\n", "new\n"))
            self.assertEqual(stat.S_IMODE(real.stat().st_mode), 0o644)


class CliTests(unittest.TestCase):
    def test_three_arguments_and_defaults(self):
        args = app.parse_args([])
        self.assertEqual((args.output, args.num_teams, args.loglevel), ("-", 5, "WARNING"))
        args = app.parse_args(["result.yaml", "-n", "2", "-l", "debug"])
        self.assertEqual((args.output, args.num_teams, args.loglevel), ("result.yaml", 2, "DEBUG"))

    def test_main_builds_the_live_client_by_default(self):
        # Every other CLI test injects a client, so this guards the production wiring.
        # basicConfig is stubbed only to leave the test runner's root logger untouched.
        with patch.object(app.logging, "basicConfig"), \
                patch.object(app, "build_report", side_effect=app.ApiError("stop")) as build:
            self.assertEqual(app.main(["-n", "1"]), 1)
        client = build.call_args[0][0]
        self.assertEqual((client.base_url, client.interval, client.sleep, client.clock),
                         (app.API_URL, app.REQUEST_INTERVAL, app.time.sleep, app.time.monotonic))

    def test_invalid_arguments_exit_2_before_touching_output(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "existing.yaml"
            target.write_text("keep me")
            for args in ([str(target), "-n", "0"], [str(target), "-n", "abc"],
                         [str(Path(folder) / "missing" / "out.yaml")], [folder]):
                result = subprocess.run([sys.executable, "-B", "template.py"] + args, cwd=ROOT,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual((result.returncode, result.stdout), (2, ""))
                self.assertEqual(target.read_text(), "keep me")


class EndToEndTests(unittest.TestCase):
    """Real CLI subprocess -> real urllib -> loopback HTTP -> YAML, file and stderr."""

    @classmethod
    def setUpClass(cls):
        cls.requests, cls.routes = [], {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.requests.append(self.path)
                status, body = cls.routes.get(self.path, (404, b""))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        del self.requests[:]
        self.routes.clear()
        self.route("/proPlayers", [player(1, 10), player(2, 20, "2026-01-02T00:00:00Z"),
                                   player(3, 10, None), player(4, 0)])
        self.route("/teams/10", {"team_id": 10, "name": "Winner", "wins": 7, "losses": 2, "rating": 1500})

    def route(self, path, payload, status=200):
        self.routes[path] = (status, payload if isinstance(payload, bytes) else json.dumps(payload).encode())

    def run_cli(self, *args, now="datetime(2026, 1, 3, tzinfo=timezone.utc)"):
        # The loopback client is injected through main(); only the clock is stubbed, so
        # main's own reference-time line still runs. The public CLI needs neither.
        driver = ('import sys, template; from datetime import datetime, timezone; '
                  'template.datetime = type("Clock", (datetime,), '
                  '{"now": classmethod(lambda cls, tz=None: %s)}); '
                  'client = template.ApiClient("http://127.0.0.1:%d", interval=0, sleep=lambda seconds: None); '
                  'sys.exit(template.main(client=client))') % (now, self.server.server_port)
        return subprocess.run([sys.executable, "-B", "-c", driver] + list(args), cwd=ROOT,
                              capture_output=True, text=True, timeout=30)

    def test_stdout_yaml_and_stderr_logs_are_separate(self):
        result = self.run_cli("-n", "1", "-l", "DEBUG")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('team_name: "Winner"', result.stdout)
        self.assertIn("\n    team_experience: 172800000000\n", result.stdout)  # Whole line, not a prefix.
        self.assertIn("DEBUG:", result.stderr)
        self.assertNotIn("DEBUG:", result.stdout)
        self.assertEqual(self.requests, ["/proPlayers", "/teams/10"])

    def test_default_level_warns_only_about_fidelity_loss(self):
        result = self.run_cli("-n", "2")
        warnings = [line for line in result.stderr.splitlines() if line.startswith("WARNING")]
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(warnings), 2, warnings)  # 1 unusable history; 1 team without details.
        self.assertNotIn("no team_id", result.stderr)
        self.assertIn("1 of 4 player records have no team_id", self.run_cli("-n", "1", "-l", "INFO").stderr)

    def test_file_destination_and_quiet_logging(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "result.yaml"
            result = self.run_cli(str(target), "-n", "1", "-l", "ERROR")
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            self.assertIn('team_name: "Winner"', target.read_text(encoding="utf-8"))

    def test_primary_failure_preserves_existing_output(self):
        self.route("/proPlayers", {"error": "forbidden"}, 403)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "result.yaml"
            target.write_text("keep me")
            result = self.run_cli(str(target), "-n", "1")
            self.assertEqual((result.returncode, result.stdout), (1, ""))
            self.assertIn("ERROR: /proPlayers: HTTP 403", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(target.read_text(), "keep me")

    def test_team_detail_failure_keeps_ranking_with_nulls(self):
        self.route("/teams/10", b"", 503)
        result = self.run_cli("-n", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("team_id: 10\n    wins: null", result.stdout)
        self.assertIn("HTTP 503", result.stderr)
        self.assertEqual(self.requests.count("/teams/10"), 3)

    def test_conflicting_detail_identity_is_rejected_over_http(self):
        self.route("/teams/10", {"team_id": 20, "name": "Wrong team", "wins": 900})
        result = self.run_cli("-n", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("team_id: 10\n    wins: null", result.stdout)
        self.assertIn("\n    team_experience: 172800000000\n", result.stdout)
        self.assertNotIn("Wrong team", result.stdout)
        self.assertIn("team_id", result.stderr)
        self.assertEqual(self.requests, ["/proPlayers", "/teams/10"])

    def test_reference_time_keeps_microseconds(self):
        # A history 0.25 s old counts; truncating as_of to whole seconds would call it future.
        self.route("/proPlayers", [player(1, 10, "2026-01-03T00:00:00.250Z")])
        result = self.run_cli("-n", "1", now="datetime(2026, 1, 3, 0, 0, 0, 500000, tzinfo=timezone.utc)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("\n    team_experience: 250000\n", result.stdout)

    def test_schema_drift_exits_1_without_output(self):
        self.route("/proPlayers", [player(team_id="10")])
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "new.yaml"
            result = self.run_cli(str(target), "-n", "1")
            self.assertEqual(result.returncode, 1)
            self.assertIn("no eligible teams", result.stderr)
            self.assertFalse(target.exists())


@unittest.skipUnless(yaml, "PyYAML not installed (optional cross-check)")
class ParserCrossCheckTests(unittest.TestCase):
    def test_all_bmp_scalars_and_supplementary_samples_round_trip(self):
        text = "".join(chr(i) for i in range(0x10000) if not 0xd800 <= i <= 0xdfff) + "🎮\U0010ffff"
        self.assertEqual(yaml.safe_load(app.yaml_scalar(text)), text)

    def test_rendered_report_round_trips(self):
        client = Mock()
        client.get.side_effect = [[player(personaname="yes: #\u2028中文🎮", country_code=None)],
                                  {"name": "null", "wins": 3, "rating": 1e-08}]
        report = app.build_report(client, 1, AS_OF)
        self.assertEqual(yaml.safe_load(app.render_yaml(report)), report)


def setUpModule():
    # Expected diagnostics are asserted in subprocesses; keep unit output quiet.
    quiet = patch.object(app.LOG, "disabled", True)
    quiet.start()
    unittest.addModuleCleanup(quiet.stop)


if __name__ == "__main__":
    unittest.main()
