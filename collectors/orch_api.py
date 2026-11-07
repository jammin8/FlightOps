#!/usr/bin/env python3
import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

import settings

ORCH_URL = os.environ.get("FLIGHTOPS_ORCH_URL") or settings.CONFIG["orchestrator_url"]
TOKEN_FILE = os.environ.get("FLIGHTOPS_ORCH_TOKEN_FILE") or os.path.expanduser("~/.config/flightops/orch-read-token")
LOOPBACK = re.compile(r"http://(127\.0\.0\.1|localhost|\[::1\]):[0-9]{1,5}")
PROTOCOL = {settings.CONFIG["orchestrator_protocol_header"]: "2"}
SHELL = "/api/orchestration/shell"
THREADS = "/api/orchestration/threads"
REQUEST_SECONDS = 8
BODY_BYTES = 32 * 1024 * 1024
REPOSITORY = re.compile(r"github\.com/" + re.escape(settings.OWNER.lower()) + "/("
                        + "|".join(re.escape(v.lower()) for v in settings.REPOS.values()) + ")")
REPO_KEYS = {v.lower(): k for k, v in settings.REPOS.items()}


class Down(Exception):
    pass


class Refused(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Malformed(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


open_local = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open


class TooLarge(Malformed):
    pass


def get(base, token, path, opener, limit=BODY_BYTES):
    headers = {"Accept": "application/json", "Authorization": "Bearer " + token} | PROTOCOL
    request = urllib.request.Request(base.rstrip("/") + path, headers=headers, method="GET")
    try:
        with opener(request, timeout=REQUEST_SECONDS) as answer:
            body = answer.read(limit + 1)
    except urllib.error.HTTPError as error:
        error.close()
        raise Refused(error.code) from None
    except (urllib.error.URLError, http.client.HTTPException, OSError):
        raise Down() from None
    if len(body) > limit:
        raise TooLarge()
    try:
        decoded = json.loads(body)
    except ValueError:
        raise Malformed() from None
    if not isinstance(decoded, dict):
        raise Malformed()
    return decoded


def read_token(path):
    try:
        with open(path) as f:
            return f.read().strip() or None
    except OSError:
        return None


def project_names(shell):
    names = {}
    for project in shell["projects"]:
        key = (project.get("repositoryIdentity") or {}).get("canonicalKey")
        match = REPOSITORY.fullmatch(key.lower()) if isinstance(key, str) else None
        if match:
            names[project["id"]] = REPO_KEYS[match.group(1)]
    return names
