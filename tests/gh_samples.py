
import datetime
import importlib.util
import os
import sys

COLLECTORS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "collectors")
sys.path.insert(0, COLLECTORS)


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(COLLECTORS, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


T0 = 1792800000.0


def commit(oid, *, parents=1, headline="work", body="", committed=T0):
    return {"oid": oid, "committedDate": iso(committed), "messageHeadline": headline, "messageBody": body,
            "parents": {"totalCount": parents}, "statusCheckRollup": None}


def suite(created, *runs, event="pull_request", workflow="ci", app="github-actions", run_id=7, more=False, branch="feat", app_id=None):
    return {"createdAt": iso(created), "app": {"slug": app, "databaseId": app_id}, "branch": {"name": branch} if branch else None,
            "workflowRun": {"databaseId": run_id, "event": event, "createdAt": iso(created), "workflow": {"name": workflow}} if event else None,
            "checkRuns": {"pageInfo": {"hasNextPage": more}, "nodes": list(runs)}}


def check(name, state, at, *, required=True, i=1, status="COMPLETED"):

    return {"databaseId": i, "name": name, "status": status, "conclusion": state if status == "COMPLETED" else None,
            "startedAt": iso(at) if at is not None else None,
            "completedAt": iso(at + 60) if status == "COMPLETED" and at is not None else None, "isRequired": required}


def hydrated(*suites, statuses=(), more=False):
    return {"checkSuites": {"pageInfo": {"hasNextPage": more}, "nodes": list(suites)}, "status": {"contexts": list(statuses)}}


def passing(at, *, event="pull_request", workflow="ci", run_id=7, state="SUCCESS", branch="feat"):
    return hydrated(suite(at, check("fast", state, at, i=run_id * 10), check("integration", "SUCCESS", at, i=run_id * 10 + 1),
                          event=event, workflow=workflow, run_id=run_id, branch=branch))


def status(context, state, at, required=True, creator=None):

    return {"context": context, "state": state, "createdAt": iso(at), "isRequired": required,
            "creator": {"__typename": creator[0], "login": creator[1]} if creator else None}



RULES = [("github-actions", "fast", None), ("github-actions", "integration", None)]


def protection(*checks, pattern="main"):

    return {"branchProtectionRules": {"pageInfo": {"hasNextPage": False}, "nodes": [
                {"pattern": pattern, "requiresStatusChecks": True, "requiredStatusCheckContexts": [c[1] for c in checks],
                 "requiredStatusChecks": [{"context": c[1], "app": {"slug": c[0]} if c[0] else None} for c in checks]}]},
            "rulesets": {"pageInfo": {"hasNextPage": False}, "nodes": []}}


def pr(commits, *, number=1, ref="feat", created=T0, draft=False, timeline=(), merged=None, state="OPEN", updated=None):
    return {"id": f"PR_{number}", "number": number, "state": state, "isDraft": draft, "createdAt": iso(created),
            "updatedAt": iso(updated or created), "mergedAt": iso(merged) if merged else None, "closedAt": iso(merged) if merged else None,
            "additions": 100, "deletions": 20, "changedFiles": 4, "headRefName": ref, "headRefOid": commits[-1]["oid"] if commits else None,
            "baseRefName": "main",
            "commits": {"pageInfo": {"hasNextPage": False}, "nodes": [{"commit": c} for c in commits]},
            "timelineItems": {"pageInfo": {"hasNextPage": False}, "nodes": list(timeline)}}


def ready(at):
    return {"__typename": "ReadyForReviewEvent", "createdAt": iso(at)}


def drafted(at):
    return {"__typename": "ConvertToDraftEvent", "createdAt": iso(at)}


def receipts(*, opened=None, pushes=(), coverage=()):

    return {"opened": {n: [{"sha": sha, "t": T0, "id": f"o{n}"}] for n, sha in (opened or {}).items()},
            "pushes": [{"ref": "refs/heads/" + ref, "before": "0" * 40, "head": head, "t": t, "id": f"p{i}"}
                       for i, (ref, head, t) in enumerate(pushes)],
            "coverage": [list(c) for c in coverage]}


A, B, C, M = "a" * 40, "b" * 40, "c" * 40, "m" * 40
