import collections
import math
import os
import selectors
import signal
import subprocess
import threading
import time

CHUNK = 65536
CLOCK_SKEW = 5
GRACE = 1
FORCED_GRACE = 0.25
FD_FOLDERS = ("/proc/self/fd", "/dev/fd")
Answer = collections.namedtuple("Answer", "returncode stdout")


class TooLarge(Exception):
    pass


def cache_current(path, payload, ttl, now=None):
    now = time.time() if now is None else now
    fetched = payload.get("fetched") if isinstance(payload, dict) else None
    if type(fetched) not in (int, float) or not math.isfinite(fetched):
        return False
    return 0 <= now - os.path.getmtime(path) < ttl and -CLOCK_SKEW <= now - fetched < ttl


def send(group, sign):
    try:
        os.killpg(group, sign)
        return True
    except OSError:
        return False


def end_group(process, grace=GRACE, clock=time.monotonic):
    send(process.pid, signal.SIGTERM)
    deadline = clock() + grace
    while clock() < deadline:
        if process.poll() is not None and not send(process.pid, 0):
            return
        time.sleep(0.01)
    send(process.pid, signal.SIGKILL)
    process.wait()


class Terminated(SystemExit):
    pass


def terminated(*_):
    raise Terminated(128 + signal.SIGTERM)


def open_descriptors():
    for folder in FD_FOLDERS:
        try:
            return [int(name) for name in os.listdir(folder)]
        except (OSError, ValueError):
            continue
    return range(os.sysconf("SC_OPEN_MAX"))


def guard(lifeline, writer):
    def start():
        if os.fork() != 0:
            return
        try:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.close(writer)
            for descriptor in open_descriptors():
                if descriptor != lifeline:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            os.read(lifeline, 1)
            os.killpg(0, signal.SIGKILL)
        finally:
            os._exit(0)
    return start


def collect(process, command, limit, timeout, clock):
    deadline = clock() + timeout
    chunks, size = [], 0
    with process.stdout, selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            left = deadline - clock()
            if left <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            if not selector.select(left):
                continue
            chunk = os.read(process.stdout.fileno(), CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise TooLarge()
            chunks.append(chunk)
    return Answer(process.wait(max(0, deadline - clock())), b"".join(chunks))


def run(command, limit, timeout, clock=time.monotonic):
    watching = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, terminated) if watching else None
    lifeline, held = os.pipe()
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True, preexec_fn=guard(lifeline, held))
        os.close(lifeline)
        lifeline = None
        grace = GRACE
        try:
            return collect(process, command, limit, timeout, clock)
        except Terminated:
            grace = FORCED_GRACE
            raise
        finally:
            end_group(process, grace)
    finally:
        os.close(held)
        if lifeline is not None:
            os.close(lifeline)
        if watching:
            signal.signal(signal.SIGTERM, previous)
