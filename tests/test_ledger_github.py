import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collectors"))
import ledger_fakes
import ledger_github
from ledger_fakes import FakeGitHub, http

PATH = "repos/example-org/example-web/activity?per_page=100"


def client(fake, **kwargs):
    made = ledger_github.Client(runner=fake, **kwargs)
    made.start()
    return made


class Replies(unittest.TestCase):
    def test_a_304_is_read_from_the_status_line_though_gh_exits_one(self):
        fake = FakeGitHub({PATH: lambda p, e: http(304)})
        self.assertTrue(client(fake).rest(PATH, etag='W/"a"').not_modified)

    def test_the_stored_etag_is_sent_verbatim(self):
        fake = FakeGitHub({PATH: {}})
        client(fake).rest(PATH, etag='W/"a"')
        self.assertEqual(fake.calls, [(PATH, 'W/"a"')])

    def test_the_next_cursor_comes_from_a_foreign_looking_link(self):
        fake = FakeGitHub({PATH: http(200, [], ledger_fakes.page_link("abc"))})
        self.assertEqual(client(fake).rest(PATH).next_cursor, "abc")

    def test_a_timeout_is_unreadable_not_empty(self):
        fake = FakeGitHub({PATH: subprocess.TimeoutExpired("gh", 1)})
        with self.assertRaises(ledger_github.Unreadable):
            client(fake).rest(PATH)

    def test_a_write_or_a_foreign_path_is_refused_before_any_call(self):
        made = client(FakeGitHub())
        for path in ("repos/example-org/example-web/pulls/1/merge -X POST", "repos/other/thing/activity", "../secrets", "user"):
            with self.assertRaises(ValueError):
                made.rest(path)
        self.assertEqual(made.requests, 0)

    def test_every_rest_call_is_a_get(self):
        seen = []
        fake = FakeGitHub({PATH: {}})
        def spy(command, limit, timeout):
            seen.append(command)
            return fake(command, limit, timeout)
        client(spy).rest(PATH)
        self.assertTrue(all(c[c.index("--method") + 1] == "GET" for c in seen))

    def test_a_mutation_is_refused(self):
        with self.assertRaises(ValueError):
            client(FakeGitHub()).graphql_query("mutation { x }")


class Budget(unittest.TestCase):
    def test_no_rest_call_below_the_floor(self):
        fake = FakeGitHub({PATH: {}}, remaining=999)
        with self.assertRaises(ledger_github.Stop) as stopped:
            client(fake).rest(PATH)
        self.assertEqual((stopped.exception.reason, fake.calls), ("budget-floor", []))

    def test_the_floor_is_read_from_each_response(self):
        fake = FakeGitHub({PATH: http(200, {}, remaining=999)})
        made = client(fake)
        made.rest(PATH)
        with self.assertRaises(ledger_github.Stop):
            made.rest(PATH)
        self.assertEqual(len(fake.calls), 1)

    def test_no_graphql_call_below_the_floor(self):
        made = client(FakeGitHub())
        made.graphql = 999
        with self.assertRaises(ledger_github.Stop):
            made.graphql_query("x")

    def test_an_unknown_budget_stops_optional_reads(self):
        def broken(command, limit, timeout):
            return ledger_fakes.capture.Answer(0, http(200, {"resources": {}}))
        with self.assertRaises(ledger_github.Stop) as stopped:
            ledger_github.Client(runner=broken).start()
        self.assertEqual(stopped.exception.reason, "budget-unknown")

    def test_a_pass_stops_at_its_request_limit(self):
        fake = FakeGitHub({PATH: {}})
        made = client(fake)
        with self.assertRaises(ledger_github.Stop) as stopped:
            for _ in range(ledger_github.PASS_REQUESTS + 1):
                made.rest(PATH)
        self.assertEqual((stopped.exception.reason, len(fake.calls)), ("pass-limit", ledger_github.PASS_REQUESTS))

    def test_a_304_counts_against_the_pass_limit(self):
        fake = FakeGitHub({PATH: lambda p, e: http(304)})
        made = client(fake)
        with self.assertRaises(ledger_github.Stop) as stopped:
            for _ in range(ledger_github.PASS_REQUESTS + 1):
                made.rest(PATH, etag="x")
        self.assertEqual((stopped.exception.reason, made.requests, made.not_modified, len(fake.calls)),
                         ("pass-limit", ledger_github.PASS_REQUESTS, ledger_github.PASS_REQUESTS, ledger_github.PASS_REQUESTS))

    def test_a_secondary_limit_stops_the_pass(self):
        fake = FakeGitHub({PATH: http(403, {}, {"Retry-After": "60"})})
        with self.assertRaises(ledger_github.Stop) as stopped:
            client(fake).rest(PATH)
        self.assertEqual(stopped.exception.reason, "rate-limited")

    def test_the_deadline_stops_the_pass(self):
        now = [0]
        fake = FakeGitHub({PATH: {}})
        made = client(fake, seconds=10, clock=lambda: now[0])
        now[0] = 99
        with self.assertRaises(ledger_github.Stop) as stopped:
            made.rest(PATH)
        self.assertEqual(stopped.exception.reason, "deadline")


if __name__ == "__main__":
    unittest.main()
