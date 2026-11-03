import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402
import capture

GH = next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), "gh")
OWNER = settings.OWNER
REPOS = settings.REPOS
FLOOR = 1000
PASS_REQUESTS = 80
CALL_SECONDS = 25
BODY_LIMIT = 16 * 1024 * 1024
READ_PATH = re.compile(r"(rate_limit|repos/" + re.escape(OWNER) + "/(" + "|".join(re.escape(v) for v in REPOS.values()) + r")/[A-Za-z0-9_./-]+(\?[A-Za-z0-9_.=&%+:/-]*)?)")
STATUS_LINE = re.compile(r"HTTP/[0-9.]+ (\d{3})")
NEXT_CURSOR = re.compile(r'<[^>]*[?&]after=([^&>]+)[^>]*>; rel="next"')


class Stop(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Unreadable(Exception):
    pass


class Reply:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    @property
    def not_modified(self):
        return self.status == 304

    @property
    def ok(self):
        return self.status == 200

    @property
    def etag(self):
        return self.headers.get("etag")

    @property
    def next_cursor(self):
        match = NEXT_CURSOR.search(self.headers.get("link") or "")
        return match.group(1) if match else None


def parse(raw):
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n")
    head, _, body = text.partition("\n\n")
    lines = head.split("\n")
    status = STATUS_LINE.match(lines[0])
    if not status:
        raise Unreadable("no status line")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    try:
        parsed = json.loads(body) if body.strip() else None
    except ValueError:
        if int(status.group(1)) == 200:
            raise Unreadable("unreadable body") from None
        parsed = None
    return Reply(int(status.group(1)), headers, parsed)


def run_command(command, limit, timeout):
    return capture.run(command, limit, timeout)


class Client:
    def __init__(self, runner=run_command, clock=time.monotonic, seconds=60, floor=FLOOR):
        self.runner, self.clock, self.floor = runner, clock, floor
        self.deadline = clock() + seconds
        self.requests = self.not_modified = self.graphql_requests = self.graphql_points = 0
        self.core_start = self.core = self.graphql = None

    def left(self):
        return self.deadline - self.clock()

    def start(self):
        try:
            reply = self.call(["api", "-i", "--method", "GET", "rate_limit"])
        except Unreadable:
            raise Stop("budget-unknown") from None
        resources = reply.body.get("resources") if reply.ok and isinstance(reply.body, dict) else None
        try:
            self.core_start = self.core = int(resources["core"]["remaining"])
            self.graphql = int(resources["graphql"]["remaining"])
        except (KeyError, TypeError, ValueError):
            raise Stop("budget-unknown") from None

    def guard(self, remaining):
        if remaining is None:
            raise Stop("budget-unknown")
        if remaining < self.floor:
            raise Stop("budget-floor")
        if self.left() <= 2:
            raise Stop("deadline")

    def call(self, arguments):
        timeout = min(CALL_SECONDS, self.left())
        if timeout <= 0:
            raise Stop("deadline")
        try:
            answer = self.runner([GH] + arguments, BODY_LIMIT, timeout)
        except subprocess.TimeoutExpired:
            raise Unreadable("timed out") from None
        except capture.TooLarge:
            raise Unreadable("too large") from None
        except OSError:
            raise Unreadable("gh could not start") from None
        if not answer.stdout:
            raise Unreadable("no answer")
        return parse(answer.stdout)

    def rest(self, path, etag=None):
        if not READ_PATH.fullmatch(path):
            raise ValueError("refused path")
        self.guard(self.core)
        if self.requests >= PASS_REQUESTS:
            raise Stop("pass-limit")
        arguments = ["api", "-i", "--method", "GET", path]
        if etag:
            arguments += ["-H", f"If-None-Match: {etag}"]
        self.requests += 1
        reply = self.call(arguments)
        remaining = reply.headers.get("x-ratelimit-remaining")
        if remaining and remaining.isdigit():
            self.core = int(remaining)
        if reply.not_modified:
            self.not_modified += 1
        if reply.status == 429 or (reply.status == 403 and (remaining == "0" or "retry-after" in reply.headers)):
            raise Stop("rate-limited")
        return reply

    def graphql_query(self, query):
        if re.match(r"\s*mutation\b", query):
            raise ValueError("refused mutation")
        self.guard(self.graphql)
        self.graphql_requests += 1
        try:
            answer = self.runner([GH, "api", "graphql", "-f", "query=query { rateLimit { cost remaining } " + query + " }"], BODY_LIMIT,
                                 max(1, min(CALL_SECONDS, self.left())))
        except (subprocess.TimeoutExpired, capture.TooLarge, OSError):
            raise Unreadable("graphql unavailable") from None
        try:
            body = json.loads(answer.stdout or b"{}")
        except ValueError:
            raise Unreadable("unreadable body") from None
        if not isinstance(body, dict):
            raise Unreadable("unreadable body")
        data = body.get("data") or {}
        rate = data.get("rateLimit") or {}
        if type(rate.get("cost")) is int:
            self.graphql_points += rate["cost"]
        if type(rate.get("remaining")) is int:
            self.graphql = rate["remaining"]
        return data, body.get("errors") or []
