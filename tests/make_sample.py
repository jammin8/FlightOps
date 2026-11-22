import os
import sys
import tempfile
import time

os.environ["TZ"] = "UTC"
time.tzset()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import synthetic  # noqa: E402
import config  # noqa: E402
import metrics  # noqa: E402
import store  # noqa: E402

TARGET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "collectors", "sample-status.json")


def sample():
    with tempfile.TemporaryDirectory() as directory:
        db = store.Store(directory)
        synthetic.build_sample_store(db)
        status = metrics.build(db, config.parse({"repos": synthetic.REPOS}), synthetic.NOW)
        db.close()
    return status


if __name__ == "__main__":
    metrics.write(sample(), sys.argv[1] if len(sys.argv) > 1 else TARGET)
    print(TARGET)
