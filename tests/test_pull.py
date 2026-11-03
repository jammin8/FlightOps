import fcntl
import importlib.util
import os
import pathlib
import re
import resource
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("pull", os.path.join(os.path.dirname(__file__), "..", "collectors", "pull.py"))
pull = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pull)
import capture

WANTED_DESCRIPTORS = 8192
LOW_SOFT_LIMIT = 256
LOW_MARGIN = 56
COLLECTORS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "collectors"))


class Answer:
    def __init__(self, code, out):
        self.returncode, self.stdout = code, out


def script(body):
    return [sys.executable, "-I", "-c", body]


class Bounded(unittest.TestCase):
    def test_a_stream_past_the_limit_is_stopped_while_it_writes(self):
        started = time.monotonic()
        with self.assertRaises(capture.TooLarge):
            capture.run(script("import sys\nwhile True: sys.stdout.write('x' * 65536)"), 100000, 20)
        self.assertLess(time.monotonic() - started, 10)

    def test_a_silent_process_cannot_pass_its_deadline(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            capture.run(script("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(30)"), 100, 0.2)
        self.assertLess(time.monotonic() - started, 3)

    def test_a_descendant_holding_the_pipe_cannot_pass_the_deadline(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            capture.run(script("import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'])"), 100, 0.2)
        self.assertLess(time.monotonic() - started, 3)

    def test_output_and_exit_code_come_back(self):
        answer = capture.run(script("import sys\nsys.stdout.write('ok')\nsys.exit(3)"), 100, 5)
        self.assertEqual((answer.returncode, answer.stdout), (3, b"ok"))


def target_soft(soft, hard, wanted=WANTED_DESCRIPTORS):
    if soft == resource.RLIM_INFINITY:
        return soft
    if hard == resource.RLIM_INFINITY or hard >= wanted:
        return max(soft, wanted)
    return None


def raise_soft_limit(test):
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = target_soft(soft, hard)
    if target is None:
        test.skipTest(f"hard descriptor limit {hard} is below {WANTED_DESCRIPTORS}")
    test.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (OSError, ValueError) as error:
        test.skipTest(f"soft descriptor limit cannot reach {target}: {error}")


def lower_soft_limit(test):
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    lowered = LOW_SOFT_LIMIT if soft == resource.RLIM_INFINITY else min(soft, LOW_SOFT_LIMIT)
    if lowered <= LOW_MARGIN:
        test.skipTest(f"soft descriptor limit {lowered} leaves too little room")
    test.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
    resource.setrlimit(resource.RLIMIT_NOFILE, (lowered, hard))
    return lowered


class DescriptorLimits(unittest.TestCase):
    def test_the_soft_limit_rises_when_the_hard_limit_allows_it(self):
        infinity = resource.RLIM_INFINITY
        self.assertEqual(target_soft(256, infinity), WANTED_DESCRIPTORS)
        self.assertEqual(target_soft(256, 10240), WANTED_DESCRIPTORS)
        self.assertEqual(target_soft(16384, infinity), 16384)
        self.assertEqual(target_soft(infinity, infinity), infinity)

    def test_a_hard_limit_below_the_target_cannot_be_raised(self):
        self.assertIsNone(target_soft(256, 256))
        self.assertIsNone(target_soft(1024, 1024))

    def test_a_256_soft_limit_really_rises(self):
        lower_soft_limit(self)
        raise_soft_limit(self)
        self.assertGreaterEqual(resource.getrlimit(resource.RLIMIT_NOFILE)[0], WANTED_DESCRIPTORS)


class Children(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pidfile = os.path.join(tmp.name, "pid")
        self.addCleanup(self.spare)

    def child(self, tail, ignore_term=False):
        body = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)" if ignore_term else "import time; time.sleep(30)"
        return script("import subprocess, sys, time\n"
                      f"child = subprocess.Popen([sys.executable, '-I', '-c', {body!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                      f"open({self.pidfile!r}, 'w').write(str(child.pid))\n" + tail)

    def pid(self):
        with open(self.pidfile) as f:
            return int(f.read())

    def alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def spare(self):
        if os.path.exists(self.pidfile) and self.alive(self.pid()):
            os.kill(self.pid(), signal.SIGKILL)

    def assert_gone(self):
        pid = self.pid()
        end = time.monotonic() + 3
        while self.alive(pid) and time.monotonic() < end:
            time.sleep(0.01)
        self.assertFalse(self.alive(pid))


class GroupCleanup(Children):
    def test_a_descendant_ends_with_a_timeout(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            capture.run(self.child("time.sleep(30)"), 100, 0.3)
        self.assert_gone()

    def test_a_descendant_ends_with_a_successful_parent(self):
        self.assertEqual(capture.run(self.child("sys.stdout.write('ok')"), 100, 5), capture.Answer(0, b"ok"))
        self.assert_gone()

    def test_a_descendant_ends_with_a_failed_parent(self):
        self.assertEqual(capture.run(self.child("sys.exit(3)"), 100, 5).returncode, 3)
        self.assert_gone()

    def test_a_descendant_ends_when_the_output_is_refused(self):
        with self.assertRaises(capture.TooLarge):
            capture.run(self.child("sys.stdout.write('x' * 1000); sys.stdout.flush(); time.sleep(30)"), 100, 5)
        self.assert_gone()

    def test_a_descendant_ignoring_sigterm_ends_by_sigkill(self):
        started = time.monotonic()
        self.assertEqual(capture.run(self.child("", ignore_term=True), 100, 5).returncode, 0)
        self.assertLess(time.monotonic() - started, capture.GRACE + 2)
        self.assert_gone()

    def test_the_child_runs_in_its_own_group(self):
        answer = capture.run(script("import os, sys\nsys.stdout.write(str(os.getpgrp() == os.getpid()))"), 100, 5)
        self.assertEqual(answer.stdout, b"True")

    def test_sigterm_to_the_collector_ends_the_child_group(self):
        inner = self.child("time.sleep(30)")
        outer = [sys.executable, "-I", "-c", f"import sys\nsys.path.insert(0, {COLLECTORS!r})\nimport capture\ncapture.run({inner!r}, 100, 20)\n"]
        process = subprocess.Popen(outer)
        self.addCleanup(process.wait)
        end = time.monotonic() + 5
        while not os.path.exists(self.pidfile) and time.monotonic() < end:
            time.sleep(0.01)
        time.sleep(0.1)
        process.terminate()
        self.assertEqual(process.wait(5), 143)
        self.assert_gone()


class NestedStop(Children):
    def collector(self, tail="time.sleep(30)"):
        inner = self.child(tail, ignore_term=True)
        return [sys.executable, "-I", "-c", f"import sys\nsys.path.insert(0, {COLLECTORS!r})\nimport capture\ncapture.run({inner!r}, 100, 20)\n"]

    def started(self):
        process = subprocess.Popen(self.collector())
        self.addCleanup(process.wait)
        end = time.monotonic() + 5
        while not os.path.exists(self.pidfile) and time.monotonic() < end:
            time.sleep(0.01)
        time.sleep(0.1)
        return process

    def test_sigterm_forces_the_group_down_inside_the_outer_grace(self):
        process = self.started()
        started = time.monotonic()
        process.terminate()
        process.wait(5)
        self.assertLess(time.monotonic() - started, 1)
        self.assert_gone()

    def test_sigkill_of_the_collector_still_ends_the_group(self):
        process = self.started()
        process.kill()
        process.wait(5)
        self.assert_gone()

    def killed_with_descriptors(self, count, above):
        marker = self.pidfile + ".fd"
        inner = self.child("time.sleep(30)")
        body = ("import os, sys\n"
                f"sys.path.insert(0, {COLLECTORS!r})\nimport capture\n"
                f"held = [os.open(os.devnull, os.O_RDONLY) for _ in range({count})]\n"
                f"open({marker!r}, 'w').write(str(held[-1]))\n"
                f"capture.run({inner!r}, 100, 20)\n")
        process = subprocess.Popen([sys.executable, "-I", "-c", body])
        self.addCleanup(process.wait)
        end = time.monotonic() + 5
        while not os.path.exists(self.pidfile) and time.monotonic() < end:
            time.sleep(0.01)
        time.sleep(0.1)
        with open(marker) as f:
            self.assertGreater(int(f.read()), above)
        process.kill()
        process.wait(5)
        self.assert_gone()

    def test_sigkill_ends_the_group_with_the_lifeline_above_4095(self):
        raise_soft_limit(self)
        self.killed_with_descriptors(4100, 4095)

    def test_sigkill_ends_the_group_under_a_256_descriptor_soft_limit(self):
        soft = lower_soft_limit(self)
        self.killed_with_descriptors(soft - LOW_MARGIN, soft // 2)

    def test_every_open_descriptor_is_found_whatever_its_number(self):
        raise_soft_limit(self)
        self.assertIn(self.moved_to(5000), capture.open_descriptors())

    def test_a_descriptor_near_a_256_soft_limit_is_found(self):
        soft = lower_soft_limit(self)
        self.assertIn(self.moved_to(soft - 1), capture.open_descriptors())

    def moved_to(self, number):
        high = os.open(os.devnull, os.O_RDONLY)
        moved = fcntl.fcntl(high, fcntl.F_DUPFD, number)
        os.close(high)
        self.addCleanup(os.close, moved)
        return moved

    def test_descriptors_fall_back_to_the_real_limit(self):
        with mock.patch.object(capture, "FD_FOLDERS", ()):
            self.assertEqual(capture.open_descriptors(), range(os.sysconf("SC_OPEN_MAX")))

    def test_the_guard_keeps_nothing_but_the_lifeline_reader(self):
        report = self.pidfile + ".guard"
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        pid = os.fork()
        if pid == 0:
            try:
                os.read = lambda *_: open(report, "w").write(" ".join(sorted(os.listdir("/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"))))
                os.killpg = lambda *_: None
                capture.guard(read, write)()
            finally:
                os._exit(0)
        os.close(write)
        os.waitpid(pid, 0)
        end = time.monotonic() + 3
        while not os.path.exists(report) and time.monotonic() < end:
            time.sleep(0.01)
        with open(report) as f:
            self.assertNotIn(str(write), f.read().split())

    def test_the_forced_grace_is_shorter_than_the_outer_grace(self):
        source = pathlib.Path(COLLECTORS, "..", "Sources", "FlightOps", "Supervised.swift").resolve().read_text()
        outer = float(re.search(r"static let grace: TimeInterval = ([0-9.]+)", source).group(1))
        self.assertLessEqual(capture.FORCED_GRACE * 2, outer)

    def test_the_lifeline_does_not_hold_stdout_open(self):
        self.assertEqual(capture.run(script("import sys\nsys.stdout.write('ok')"), 100, 5), capture.Answer(0, b"ok"))


class Pull(unittest.TestCase):
    def test_it_only_reads_the_published_file_over_the_existing_path(self):
        seen = []
        data = pull.pull(lambda command, **_: seen.append(command) or Answer(0, b"{}"))
        self.assertEqual(data, b"{}")
        self.assertEqual(seen[0][0], "ssh")
        self.assertEqual(seen[0][-3:], ["flightops-vm", "cat", ".local/state/flightops/flightops-status.json"])
        self.assertIn("BatchMode=yes", seen[0])

    def test_a_failed_pull_gives_no_bytes(self):
        self.assertIsNone(pull.pull(lambda *_, **__: Answer(255, b"")))

    def test_an_empty_answer_gives_no_bytes(self):
        self.assertIsNone(pull.pull(lambda *_, **__: Answer(0, b"")))

    def test_an_oversized_answer_gives_no_bytes(self):
        def huge(*_, **__):
            raise capture.TooLarge()
        self.assertIsNone(pull.pull(huge))

    def test_the_read_is_bounded_by_the_snapshot_limit(self):
        seen = []
        pull.pull(lambda command, limit, timeout: seen.append((limit, timeout)) or Answer(0, b"{}"))
        self.assertEqual(seen, [(pull.LIMIT, pull.SECONDS)])

    def test_a_timeout_gives_no_bytes(self):
        def slow(*_, **__):
            raise subprocess.TimeoutExpired("ssh", 15)
        self.assertIsNone(pull.pull(slow))

    def test_the_mac_never_writes_to_the_vm(self):
        source = pathlib.Path(__file__, "..", "..", "collectors", "pull.py").resolve().read_text()
        for word in ("scp", "rsync", "tee", " > ", "rm "):
            self.assertNotIn(word, source)


if __name__ == "__main__":
    unittest.main()
