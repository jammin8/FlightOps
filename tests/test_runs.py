import unittest

from collectors import runs
from collectors.model_evidence import encode, export_bundle
from test_model_evidence_core import bundle


class ReadTimeClocks(unittest.TestCase):
    def setUp(self):
        self.cache = runs.ingest(None, encode(export_bundle(bundle())), now=1000)

    def test_a_valid_clock_reads_as_before(self):
        status = runs.status(self.cache, now=1000)
        self.assertEqual((status["state"], status["stale"], status["error"]), ("complete", False, None))
        self.assertGreater(status["source_age_s"], 0)

    def test_a_source_ahead_of_the_reader_is_never_age_zero(self):
        status = runs.status(self.cache, now=self.cache["export"]["as_of"] - 100)
        self.assertEqual((status["source_age_s"], status["stale"], status["state"], status["error"]), (None, True, "error", "future-source"))

    def test_a_publication_ahead_of_the_reader_is_future_too(self):
        status = runs.status(self.cache, now=self.cache["export"]["generated_at"] - runs.CLOCK_SKEW - 1)
        self.assertEqual((status["state"], status["stale"]), ("error", True))

    def test_the_shared_tolerance_applies(self):
        at = self.cache["export"]["generated_at"]
        self.assertIsNone(runs.status(self.cache, now=at - runs.CLOCK_SKEW)["error"])

    def test_a_stored_error_is_kept_over_a_future_clock(self):
        cache = dict(self.cache, last_error="source-invalid")
        self.assertEqual(runs.status(cache, now=1)["error"], "source-invalid")


if __name__ == "__main__":
    unittest.main()
