import os
import random
import tempfile
import unittest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "collectors"))
import bottlenecks  # noqa: E402
import store  # noqa: E402
from store import iso  # noqa: E402

START = 1790000000 - 1790000000 % 86400
REPO = "acme/api"


def jobs(db, rows):
    with db.tx():
        for job_id, (queued, wait, run) in enumerate(rows, 1):
            db.db.execute("""INSERT INTO ci_jobs(repo, run_id, run_attempt, job_id, name, status, conclusion, queued_at, started_at, completed_at)
                             VALUES (?, ?, 1, ?, 'test', 'completed', 'success', ?, ?, ?)""",
                          (REPO, job_id, job_id, iso(queued), iso(queued + wait), iso(queued + wait + run)))


def history(waves=(), jam=None):
    rng = random.Random(7)
    rows = []
    for day in range(60):
        count = 160 if day in waves else 50
        for _ in range(count):
            queued = START + day * 86400 + rng.randint(8 * 3600, 22 * 3600)
            wait = rng.choice([rng.randint(5, 60)] * 9 + [rng.randint(60, 200)])
            if jam and day == jam:
                wait = rng.randint(60, 300) + max(0, queued - START - day * 86400 - 15 * 3600) // 12
            elif jam and jam < day <= jam + 3:
                wait = rng.randint(1800, 4 * 3600 if day < jam + 3 else 9000)
            rows.append((queued, wait, rng.randint(120, 600)))
    return rows


class Replay(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = store.Store(self.dir.name)

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def read(self, first, last):
        days = bottlenecks.replay(self.db, [REPO], START + first * 86400, START + last * 86400 + 86399)
        return {day - START // 86400: row["state"] for (day, stage), row in days.items() if stage == "waiting_ci"}

    def test_a_wave_flows_and_a_jam_warns_then_sticks(self):
        jobs(self.db, history(waves=(40, 42), jam=50))
        read = self.read(38, 56)
        self.assertEqual(read[40], "busy")
        self.assertEqual(read[42], "busy")
        self.assertIn(read[49], ("clear", "busy"))
        self.assertEqual(read[50], "slowing")
        self.assertEqual([read[d] for d in (51, 52, 53)], ["stuck"] * 3)

    def test_too_little_history_is_learning(self):
        jobs(self.db, [(START + i * 3600, 30, 60) for i in range(10)])
        self.assertEqual(self.read(0, 0)[0], "learning")

    def test_cancelled_before_start_is_not_waiting(self):
        with self.db.tx():
            self.db.db.execute("""INSERT INTO ci_jobs(repo, run_id, run_attempt, job_id, status, conclusion, queued_at)
                                  VALUES (?, 1, 1, 1, 'completed', 'cancelled', ?)""", (REPO, iso(START)))
        self.assertEqual(bottlenecks.gather(self.db, [REPO])["waiting_ci"], [])


class Status(unittest.TestCase):
    def test_the_view_carries_verdicts_and_the_log(self):
        import make_sample
        view = make_sample.sample()["views"]["acme/api"]
        verdicts = {s["id"]: s["verdict"] for s in view["stages"]}
        self.assertEqual({k for k, v in verdicts.items() if v}, set(bottlenecks.JUDGED))
        self.assertEqual(verdicts["waiting_ci"]["state"], "slowing")
        self.assertEqual(verdicts["waiting_ci"]["oldest_pr"], "acme/api#418")
        self.assertTrue(all(e["hours_lost"] >= 0 for e in view["bottlenecks"]))
        self.assertEqual(view["bottlenecks"], sorted(view["bottlenecks"], key=lambda e: -e["hours_lost"]))


if __name__ == "__main__":
    unittest.main()
