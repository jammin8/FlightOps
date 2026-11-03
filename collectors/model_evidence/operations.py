from collections import defaultdict
import posixpath

from . import shell
from .contract import (ContractError, MAX_TEXT, REPO, SHA, digest, identifier,
                       number, provenance, require, scoped)


def supported_creation(command, op, *, observed=False):
    commands = shell.structure(command)
    args = shell.git_sub(commands[op["index"]]["argv"])[1]
    flags = {"--allow-empty", "--allow-empty-message", "--no-verify", "--no-gpg-sign", "--signoff", "-s", "-a", "--all", "--no-edit", "--no-ff"}
    if observed:
        flags.add("--amend")
    values = {"-m", "--message", "-F", "--file"}
    i = 0
    while i < len(args):
        word = args[i]
        if word in values:
            i += 2
            if i > len(args):
                return False
            continue
        if any(word.startswith(flag + "=") for flag in values if flag.startswith("--")):
            i += 1
            continue
        if word.startswith("-") and word not in flags:
            return False
        i += 1
    return True


def derive_operations(bundle, sources, runs, errors):
    calls, completions, observations = defaultdict(list), defaultdict(list), defaultdict(list)
    for collection, target in (("calls", calls), ("completions", completions), ("observations", observations)):
        for index, row in enumerate(bundle[collection]):
            try:
                require(isinstance(row, dict), "call-shape")
                key = scoped(row.get("provider_namespace"), row.get("native_call_id"))
                require(identifier(row.get("native_session_id")), "call-session")
                target[(key, row["native_session_id"])].append(row)
            except ContractError as exc:
                errors.append({"collection": collection, "index": index, "code": str(exc)})
    receipts = []
    for call_index, ((call_id, session), copies) in enumerate(sorted(calls.items())):
        receipt_start = len(receipts)
        try:
            require(len(copies) == 1, "duplicate-call")
            c = copies[0]
            ev = provenance(c.get("evidence"), sources)
            owner = scoped(c.get("provider_namespace"), c.get("native_run_id"))
            require(owner in runs and runs[owner]["native_session_id"] == session, "call-owner")
            turn = c.get("native_turn_id")
            require(turn in {t["native_turn_id"] for t in runs[owner]["turns"]}, "call-turn")
            root, seen = owner, set()
            while runs[root]["parent"]:
                require(root not in seen, "parent-cycle")
                seen.add(root)
                p = runs[root]["parent"]
                require(p["run_id"] in runs and p["native_turn_id"] in {t["native_turn_id"] for t in runs[p["run_id"]]["turns"]}, "parent-unresolved")
                root = p["run_id"]
            command = c.get("command")
            require(isinstance(command, str) and len(command) <= 8192, "command-size")
            cwd, repo = c.get("worktree"), c.get("repository")
            require(isinstance(cwd, str) and cwd.startswith("/") and posixpath.normpath(cwd) == cwd
                    and all(identifier(part) for part in cwd.split("/")[1:]), "worktree")
            require(isinstance(repo, str) and REPO.fullmatch(repo), "repository")
            inventory = bundle.get("repositories", {}).get(repo)
            require(isinstance(inventory, dict) and inventory.get("worktree") == cwd, "repository-inventory")
            repo_ev = provenance(inventory.get("evidence"), sources)
            known = inventory.get("objects")
            require(isinstance(known, list) and all(isinstance(h, str) and SHA.fullmatch(h) for h in known), "object-inventory")
            known = set(known)
            finals = [x for x in completions[(call_id, session)] if x.get("final") is True]
            require(len(finals) == 1, "completion-ambiguous-or-missing")
            done = finals[0]
            require(identifier(done.get("completion_id")), "completion-id")
            require(type(done.get("exit_code")) is int and done["exit_code"] == 0, "completion-not-success")
            completion_ev = provenance(done.get("evidence"), sources)
            require(completion_ev["at"] >= ev["at"], "completion-before-call")
            require(done.get("process_session_id") == c.get("process_session_id") and
                    (c.get("process_session_id") is None or identifier(c["process_session_id"])), "process-session")
            output = done.get("output")
            require(isinstance(output, str) and len(output) <= MAX_TEXT, "output-size")
            ops = shell.operations(command, cwd)
            require(ops is not None, "unsupported-shell")
            base = {"call_id": call_id, "completion_id": done["completion_id"], "native_session_id": session,
                    "owning_run_id": owner, "owning_native_turn_id": turn, "root_run_id": root,
                    "repository": repo, "worktree": cwd, "call_digest": digest(c), "completion_digest": digest(done),
                    "evidence": ev, "completion_evidence": completion_ev, "repository_evidence": repo_ev,
                    "basis": "fixture-receipt", "state": "proven-in-fixture"}
            obs = observations[(call_id, session)]
            if obs:
                require(len(obs) == 1, "observation-conflict")
                ob = obs[0]
                ob_ev = provenance(ob.get("evidence"), sources)
                candidates = [o for o in ops if o["op"] in ("commit", "merge", "unresolved")]
                require(len(candidates) == 1, "observation-operation")
                op = candidates[0]
                require(op["op"] in ("commit", "merge") and op["certain"] and op["creates"] and op["same_repo"] and supported_creation(command, op, observed=True), "observation-operation")
                require(ob.get("repository") == repo and ob.get("completion_id") == done["completion_id"], "observation-binding")
                before, after = ob.get("before_sha"), ob.get("after_sha")
                require(before in known and after in known and before != after, "observation-no-creation")
                parents = ob.get("parents")
                require(isinstance(parents, list) and len(parents) == (2 if op["op"] == "merge" else 1)
                        and parents[0] == before and all(p in known for p in parents), "observation-parents")
                require(ev["at"] <= ob_ev["at"] <= completion_ev["at"], "observation-time")
                receipts.append({**base, "operation": op["op"], "sha": after, "destination_ref": None,
                                 "remote": None, "proof": "fixture-pre-post-creation", "observation_evidence": ob_ev})
            else:
                printed = shell.printed_commit(command, output, cwd)
                if printed:
                    creation = next(o for o in ops if o["op"] in ("commit", "merge"))
                    require(supported_creation(command, creation), "unsupported-creation-options")
                    sha = shell.resolve(printed[0], known)
                    require(sha is not None, "ambiguous-or-unknown-object")
                    kind = next(o["op"] for o in ops if o["op"] in ("commit", "merge"))
                    receipts.append({**base, "operation": kind, "sha": sha, "destination_ref": None,
                                     "remote": None, "proof": "log-" + printed[1].replace(" ", "-"), "observation_evidence": None})
                for push in shell.pushes(command, output, cwd):
                    sha = shell.resolve(push["new"], known)
                    require(sha is not None, "ambiguous-or-unknown-object")
                    require(identifier(push["dst"]) and (push["remote"] is None or identifier(push["remote"])), "push-destination")
                    receipts.append({**base, "operation": "push", "sha": sha,
                                     "destination_ref": "refs/heads/" + push["dst"], "remote": push["remote"],
                                     "proof": "log-push-range", "observation_evidence": None})
            if not any(r["call_id"] == call_id and r["native_session_id"] == session for r in receipts):
                raise ContractError("no-proven-operation")
        except (ContractError, TypeError, KeyError) as exc:
            del receipts[receipt_start:]
            errors.append({"collection": "calls", "index": call_index,
                           "code": str(exc) if isinstance(exc, ContractError) else "call-shape"})
    for collection, rows in (("completions", completions), ("observations", observations)):
        if set(rows) - set(calls):
            errors.append({"collection": collection, "index": 0, "code": "orphan-call-evidence"})
    owners = defaultdict(set)
    for r in receipts:
        if r["operation"] in ("commit", "merge"):
            owners[(r["repository"], r["sha"])].add(r["owning_run_id"])
    for r in receipts:
        if r["operation"] in ("commit", "merge") and len(owners[(r["repository"], r["sha"])]) != 1:
            r["state"] = "ambiguous-owner"
    return receipts
