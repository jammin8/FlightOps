import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402

PLACES = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh")
FLOOR = 500
CALL_SECONDS = 30
BODY_LIMIT = 16 * 1024 * 1024
READ_PATH = re.compile(r"(rate_limit|user|user/repos(\?[A-Za-z0-9_.=&%+:,-]*)?|repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(/[A-Za-z0-9_./-]+)?(\?[A-Za-z0-9_.=&%+:,<>-]*)?)")
STATUS_LINE = re.compile(r"HTTP/[0-9.]+ (\d{3})")


def binary():
    return os.environ.get("FLIGHTOPS_GH") or next((p for p in PLACES if os.path.exists(p)), None) or shutil.which("gh") or "gh"


class Stop(Exception):
    pass


class Unreadable(Exception):
    pass


class Reply:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    @property
    def ok(self):
        return self.status == 200

    @property
    def not_modified(self):
        return self.status == 304

    @property
    def etag(self):
        return self.headers.get("etag")


def parse(raw):
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n")
    head, _, body = text.partition("\n\n")
    while head.startswith("HTTP/") and body.startswith("HTTP/"):
        head, _, body = body.partition("\n\n")
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
    def __init__(self, runner=run_command, clock=time.monotonic, seconds=90, floor=FLOOR):
        self.runner, self.clock, self.floor = runner, clock, floor
        self.deadline = clock() + seconds
        self.requests = self.not_modified = self.graphql_requests = 0
        self.core = self.graphql = None

    def left(self):
        return self.deadline - self.clock()

    def call(self, arguments):
        timeout = min(CALL_SECONDS, self.left())
        if timeout <= 0:
            raise Stop("deadline")
        try:
            answer = self.runner([binary()] + arguments, BODY_LIMIT, timeout)
        except subprocess.TimeoutExpired:
            raise Unreadable("timed out") from None
        except capture.TooLarge:
            raise Unreadable("too large") from None
        except OSError:
            raise Unreadable("gh is not installed") from None
        if not answer.stdout:
            raise Unreadable("gh is not signed in" if answer.returncode == 4 else "no answer from gh")
        return answer

    def start(self):
        reply = self.rest("rate_limit", budget=False)
        resources = reply.body.get("resources") if reply.ok and isinstance(reply.body, dict) else None
        try:
            self.core = int(resources["core"]["remaining"])
            self.graphql = int(resources["graphql"]["remaining"])
        except (KeyError, TypeError, ValueError):
            raise Unreadable("gh is not signed in" if reply.status == 401 else "rate limit unreadable") from None

    def spare(self, remaining):
        return remaining is None or remaining >= self.floor

    def rest(self, path, etag=None, budget=True):
        if not READ_PATH.fullmatch(path):
            raise ValueError("refused path")
        if budget and not self.spare(self.core):
            raise Stop("budget")
        arguments = ["api", "-i", "--method", "GET", path]
        if etag:
            arguments += ["-H", f"If-None-Match: {etag}"]
        self.requests += 1
        reply = parse(self.call(arguments).stdout)
        remaining = reply.headers.get("x-ratelimit-remaining")
        if remaining and remaining.isdigit():
            self.core = int(remaining)
        if reply.not_modified:
            self.not_modified += 1
        if reply.status == 429 or (reply.status == 403 and (remaining == "0" or "retry-after" in reply.headers)):
            raise Stop("rate-limited")
        return reply

    def graphql_query(self, query, variables=None):
        if re.match(r"\s*mutation\b", query):
            raise ValueError("refused mutation")
        if not self.spare(self.graphql):
            raise Stop("budget")
        self.graphql_requests += 1
        arguments = ["api", "graphql", "-f", "query=" + query]
        for key, value in (variables or {}).items():
            arguments += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
        answer = self.call(arguments)
        try:
            body = json.loads(answer.stdout or b"{}")
        except ValueError:
            raise Unreadable("unreadable body") from None
        if not isinstance(body, dict):
            raise Unreadable("unreadable body")
        if "data" not in body and not body.get("errors"):
            raise Unreadable(str(body.get("message") or "query refused")[:200])
        data = body.get("data") or {}
        rate = data.get("rateLimit") or {}
        if type(rate.get("remaining")) is int:
            self.graphql = rate["remaining"]
        errors = body.get("errors") or []
        if not data and errors:
            raise Unreadable(str((errors[0] or {}).get("message") or "query refused")[:200])
        return data, errors
