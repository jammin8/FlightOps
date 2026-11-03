import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collectors"))
import capture

CORE = 5000
FEED = re.compile(r"/actions/runs\?(created=|per_page=100&page=)")


def http(status, body=None, headers=None, remaining=CORE):
    lines = [f"HTTP/2.0 {status}", f"X-Ratelimit-Remaining: {remaining}"] + [f"{k}: {v}" for k, v in (headers or {}).items()]
    return ("\r\n".join(lines) + "\r\n\r\n" + ("" if body is None else json.dumps(body))).encode()


def page_link(cursor):
    return {"Link": f'<https://api.github.com/repositories/1/activity?per_page=100&after={cursor}>; rel="next"'}


class FakeGitHub:
    def __init__(self, routes=None, graphql=None, remaining=CORE):
        self.routes, self.graphql, self.remaining = routes or {}, graphql, remaining
        self.calls, self.queries = [], []

    def __call__(self, command, limit, timeout):
        if "graphql" in command:
            self.queries.append(command[-1])
            return capture.Answer(0, json.dumps(self.graphql(command[-1]) if self.graphql else {"data": {}}).encode())
        path = command[command.index("GET") + 1]
        etag = next((a.split(": ", 1)[1] for a in command if a.startswith("If-None-Match")), None)
        if path == "rate_limit":
            return capture.Answer(0, http(200, {"resources": {"core": {"remaining": self.remaining}, "graphql": {"remaining": self.remaining}}}, remaining=self.remaining))
        if path not in self.routes and FEED.search(path):
            return capture.Answer(0, http(200, {"total_count": 0, "workflow_runs": []}, remaining=self.remaining))
        self.calls.append((path, etag))
        answer = self.routes.get(path)
        if callable(answer):
            answer = answer(path, etag)
        if answer is None:
            return capture.Answer(1, http(404, {"message": "Not Found"}, remaining=self.remaining))
        if isinstance(answer, Exception):
            raise answer
        return capture.Answer(0, answer if isinstance(answer, bytes) else http(200, answer, remaining=self.remaining))
