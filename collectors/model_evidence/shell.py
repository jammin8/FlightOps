import posixpath as path


GLOBAL_OPTS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
OPS = ("commit", "merge", "push")
WORD = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")


LITERAL_HEREDOCS = True
HEREDOC_INPUT = ("git", "cat", "tee")


def delimiter_at(cmd, i):
    j, n = i + 2, len(cmd)
    strip = j < n and cmd[j] == "-"
    j += strip
    while j < n and cmd[j] in " \t":
        j += 1
    if j < n and cmd[j] in "'\"":
        k = cmd.find(cmd[j], j + 1)
        if k < 0 or k == j + 1 or "\n" in cmd[j + 1:k]:
            return None
        found = k + 1, strip, cmd[j + 1:k], False
    else:
        escaped = j < n and cmd[j] == "\\"
        k = j + escaped
        while k < n and (cmd[k] in WORD or cmd[k] in ".-"):
            k += 1
        if k == j + escaped:
            return None
        found = k, strip, cmd[j + escaped:k], not escaped
    if found[0] < n and cmd[found[0]] not in " \t\n;&|<>)":
        return None
    return found


def literal_substitutions(cmd):
    out, i = [], 0
    while True:
        j = cmd.find("$(", i)
        if j < 0:
            return "".join(out) + cmd[i:]
        k = j + 2
        while k < len(cmd) and cmd[k] in " \t\n":
            k += 1
        m = k + 3
        if cmd.startswith("cat", k) and m < len(cmd) and cmd[m] in " \t":
            while m < len(cmd) and cmd[m] in " \t":
                m += 1
            if m < len(cmd) and cmd[m] == "\x02":
                d = m + 1
                while d < len(cmd) and cmd[d].isdigit():
                    d += 1
                e = d
                while e < len(cmd) and cmd[e] in " \t\n":
                    e += 1
                if d > m + 1 and e < len(cmd) and cmd[e] == ")":
                    out.append(cmd[i:j] + "\x01")
                    i = e + 1
                    continue
        out.append(cmd[i:j + 2])
        i = j + 2


def heredocs(cmd):
    out, i, n = [], 0, len(cmd)
    stack, pending, count = ["code"], [], 0
    while i < n:
        c, top = cmd[i], stack[-1]
        if top == "sq":
            out.append(c)
            i += 1
            if c == "'":
                stack.pop()
            continue
        if c == "\\" and i + 1 < n:
            out.append(cmd[i:i + 2])
            i += 2
            continue
        if cmd.startswith("$(", i) and top != "sq":
            stack.append("sub")
            out.append("$(")
            i += 2
            continue
        if top == "dq":
            if c == '"':
                stack.pop()
            out.append(c)
            i += 1
            continue
        if c == "#" and (i == 0 or cmd[i - 1] in " \t\n;&|("):
            j = cmd.find("\n", i)
            out.append(cmd[i:] if j < 0 else cmd[i:j])
            i = n if j < 0 else j
            continue
        if c == "'":
            stack.append("sq")
        elif c == '"':
            stack.append("dq")
        elif c == "(":
            stack.append("paren")
        elif c == ")" and top in ("sub", "paren"):
            stack.pop()
        elif cmd.startswith("<<<", i):
            return None
        elif cmd.startswith("<<", i):
            found = delimiter_at(cmd, i)
            if not found:
                return None
            end, strip, delim, bare = found
            pending.append((delim, strip, bare))
            out.append(f" \x02{count} ")
            count += 1
            i = end
            continue
        elif c == "\n" and pending:
            out.append(c)
            i += 1
            for delim, strip, bare in pending:
                body = []
                while True:
                    if i >= n:
                        return None
                    j = cmd.find("\n", i)
                    line = cmd[i:] if j < 0 else cmd[i:j]
                    i = n if j < 0 else j + 1
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
                    body.append(line)
                if bare and any(ch in "\n".join(body) for ch in "$`\\"):
                    return None
            pending = []
            continue
        out.append(c)
        i += 1
    return None if pending else "".join(out)


NAME_START = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_"
NAME_CHARS = NAME_START + "0123456789"


def one_word(cmd, j):
    nxt = cmd[j + 1] if j + 1 < len(cmd) else ""
    if nxt == "@":
        return False
    if nxt != "{":
        return True
    k = cmd.find("}", j + 2)
    inner = cmd[j + 2:k] if k > 0 else ""
    if inner.startswith("#") and len(inner) > 1:
        inner = inner[1:]
    return bool(inner) and (inner.isdigit() or inner in ("#", "?", "$", "!", "-", "*", "0")
                            or (inner[0] in NAME_START and all(ch in NAME_CHARS for ch in inner)))


def tokenize(cmd):
    out, word, quoted, expands, splits, i, n = [], [], False, False, False, 0, len(cmd)

    def end_word():
        nonlocal word, quoted, expands, splits
        if word or quoted:
            out.append(("word", "".join(word), expands, splits))
        word, quoted, expands, splits = [], False, False, False

    def op(o):
        end_word()
        if o == ";" and (not out or out[-1][0] == "op"):
            if out and out[-1][1] in ("&&", "||", "|", "|&"):
                return
            if not out:
                return
        out.append(("op", o))

    while i < n:
        c = cmd[i]
        if c in " \t\r":
            end_word()
            i += 1
        elif c == "\n":
            op(";")
            i += 1
        elif c == "#" and not word and not quoted:
            j = cmd.find("\n", i)
            i = n if j < 0 else j
        elif c == "\\":
            if i + 1 < n and cmd[i + 1] == "\n":
                i += 2
            elif i + 1 < n:
                word.append(cmd[i + 1])
                i += 2
            else:
                return None
        elif c == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                return None
            word.append(cmd[i + 1:j])
            quoted, i = True, j + 1
        elif c == '"':
            j = i + 1
            while j < n and cmd[j] != '"':
                if cmd[j] == "\\" and j + 1 < n and cmd[j + 1] in '$`"\\\n':
                    if cmd[j + 1] != "\n":
                        word.append(cmd[j + 1])
                    j += 2
                    continue
                if cmd[j] == "`" or cmd.startswith("$(", j):
                    return None
                if cmd[j] == "$":
                    expands = True
                    splits = splits or not one_word(cmd, j)
                word.append(cmd[j])
                j += 1
            if j >= n:
                return None
            quoted, i = True, j + 1
        elif c == "$":
            nxt = cmd[i + 1] if i + 1 < n else ""
            if nxt in ("(", "'", '"'):
                return None
            word.append(c)
            expands = splits = True
            i += 1
        elif c == "`":
            return None
        elif cmd.startswith("&&", i) or cmd.startswith("||", i) or cmd.startswith("|&", i):
            op(cmd[i:i + 2])
            i += 2
        elif c == "&" and cmd.startswith("&>", i):
            end_word()
            k = i + (3 if cmd.startswith("&>>", i) else 2)
            out.append(("redir", cmd[i:k]))
            i = k
        elif c in ";&|":
            if cmd.startswith(";;", i):
                return None
            op(c)
            i += 1
        elif c in "<>":
            if cmd.startswith("<(", i) or cmd.startswith(">(", i) or cmd.startswith("<<<", i):
                return None
            fd = "".join(word) if word and not quoted and "".join(word).isdigit() else ""
            if fd:
                word = []
            else:
                end_word()
            k = i + 1
            while k < n and cmd[k] in "<>&|" and k - i < 2:
                k += 1
            r = fd + cmd[i:k]
            if r.endswith("&"):
                m = k
                while m < n and (cmd[m].isdigit() or cmd[m] == "-"):
                    m += 1
                out.append(("redir", r + cmd[k:m]))
                out.append(("word", "", False, False))
                i = m
            else:
                out.append(("redir", r))
                i = k
        elif c in "()":
            return None
        else:
            if c in "*?[" or (c == "~" and not word and not quoted):
                expands = splits = True
            word.append(c)
            i += 1
    end_word()
    while out and out[-1] == ("op", ";"):
        out.pop()
    return out


def structure(cmd):
    if "\x01" in cmd or "\x02" in cmd:
        return None
    if "<<" in cmd:
        if not LITERAL_HEREDOCS:
            return None
        cmd = heredocs(cmd)
        if cmd is None:
            return None
        cmd = literal_substitutions(cmd)
    toks = tokenize(cmd)
    if toks is None or len(toks) > 512:
        return None
    cmds, cur, exp, skip, dest = [], [], [], None, {1: "out", 2: "out"}

    def close(op):
        w = strip_env(cur)
        loud = ("out", "unknown")
        quiet = dest[1] not in loud and (dest[2] not in loud or (bool(w) and w[0] in STDERR_ERRORS_ONLY))
        cmds.append({"argv": cur, "exp": exp, "op": op, "quiet": quiet,
                     "stdout": dest[1], "stderr": dest[2]})

    for tok in toks:
        kind, text = tok[0], tok[1]
        if kind == "redir":
            if skip is not None:
                return None
            skip = text
            continue
        if kind == "word":
            if skip is not None:
                if not redirect(dest, skip, text, tok[2]):
                    return None
                skip = None
                continue
            cur.append(text)
            exp.append((tok[2], tok[3]))
            continue
        if skip is not None or not cur:
            return None
        close(text)
        cur, exp, dest = [], [], {1: "out", 2: "out"}
    if skip is not None:
        return None
    if cur:
        close(None)
    lists, li = [], 0
    for i, c in enumerate(cmds):
        c["list"] = li
        if c["op"] in (";", "&", None):
            lists.append(c["op"])
            li += 1
    last = len(lists) - 1
    for i, c in enumerate(cmds):
        before = cmds[i - 1]["op"] if i else None
        same = [x for x in cmds if x["list"] == c["list"]]
        joins = [x["op"] for x in same[:-1]]
        c["piped"] = c["op"] in ("|", "|&") or before in ("|", "|&")
        c["certain"] = (c["list"] == last and lists[last] is None and all(j == "&&" for j in joins) and not c["piped"])
        inputs = [t for t in c["argv"] if t.startswith("\x02")]
        if inputs:
            word = strip_env([t for t in c["argv"] if not t.startswith("\x02")])
            if not word or word[0] not in HEREDOC_INPUT or not all(t[1:].isdigit() for t in inputs):
                return None
            keep = [k for k, t in enumerate(c["argv"]) if not t.startswith("\x02")]
            c["argv"], c["exp"] = [c["argv"][k] for k in keep], [c["exp"][k] for k in keep]
        word = strip_env(c["argv"])
        if not word or word[0] in ("{", "}", "!", "exec") or "$" in word[0]:
            return None
    return cmds


CAPTURED = {"/dev/stdout", "/dev/stderr", "/dev/fd/1", "/dev/fd/2", "/proc/self/fd/1", "/proc/self/fd/2"}
STDERR_ERRORS_ONLY = {"echo", "printf", "cat", "touch", "mkdir", "cp", "mv", "rm", "ln", "chmod", "tee"}


def redirect(dest, r, target, expands):
    fd = ""
    while r and r[0].isdigit():
        fd, r = fd + r[0], r[1:]
    to = "unknown" if (expands or target in CAPTURED or target.startswith(("/dev/fd/", "/proc/self/fd/"))) else "file"
    if r in ("&>", "&>>"):
        dest[1] = dest[2] = to
        return True
    n = int(fd) if fd else (0 if r.startswith("<") else 1)
    if r == "<":
        dest[n] = "unknown" if to == "out" else "file"
        return True
    if r in (">", ">>", ">|"):
        dest[n] = to
        return True
    if r.startswith((">&", "<&")):
        src = r[2:]
        if src == "-":
            dest[n] = "closed"
            return True
        if src.isdigit():
            dest[n] = dest.get(int(src), "unknown")
            return True
    return False


def simple_commands(cmd):
    s = structure(cmd)
    return None if s is None else [(c["argv"], c["op"]) for c in s]


def strip_env(tokens):
    i = 0
    while i < len(tokens) and "=" in tokens[i]:
        name = tokens[i].split("=", 1)[0]
        if not name or name[0].isdigit() or not set(name) <= WORD:
            break
        i += 1
    return tokens[i:]


def git_sub(tokens):
    tokens = strip_env(tokens)
    if not tokens or tokens[0] != "git":
        return None
    i, where = 1, None
    while i < len(tokens) and tokens[i].startswith("-"):
        if tokens[i] in GLOBAL_OPTS_WITH_ARG and i + 1 < len(tokens):
            if tokens[i] == "-C":
                where = tokens[i + 1] if where is None else path.join(where, tokens[i + 1])
            elif tokens[i] in ("--git-dir", "--work-tree"):
                return ("", [], None)
            elif tokens[i] == "-c" and tokens[i + 1].split("=", 1)[0].lower().startswith(("core.", "include")):
                return ("", [], None)
            i += 2
        elif tokens[i].startswith(("--git-dir=", "--work-tree=")):
            return ("", [], None)
        else:
            i += 1
    return (tokens[i], tokens[i + 1:], where) if i < len(tokens) else None


def gh_sub(tokens):
    tokens = strip_env(tokens)
    if len(tokens) >= 3 and tokens[0] == "gh" and tokens[1] == "pr" and tokens[2] in ("create", "ready"):
        return "pr-" + tokens[2], tokens[3:]
    return None


HEX_FORMATS = {"%H", "%h", "format:%H", "format:%h", "tformat:%H", "tformat:%h"}


def prints_head(tokens):
    g = git_sub(tokens)
    if not g:
        return False
    sub, args, _ = g
    if sub == "rev-parse":
        rest = [a for a in args if a not in ("--verify", "-q", "--quiet", "--short") and not a.startswith("--short=")]
        return rest == ["HEAD"]
    if sub not in ("log", "show"):
        return False
    fmt, one, i, rest = None, sub == "show", 0, []
    while i < len(args):
        a = args[i]
        if a.startswith(("--format=", "--pretty=")):
            fmt = a.split("=", 1)[1]
        elif a in ("--format", "--pretty") and i + 1 < len(args):
            fmt, i = args[i + 1], i + 1
        elif a == "--oneline":
            fmt = "oneline"
        elif sub == "log" and a in ("-1", "-n1", "--max-count=1"):
            one = True
        elif sub == "log" and a == "-n" and i + 1 < len(args) and args[i + 1] == "1":
            one, i = True, i + 1
        elif sub == "show" and a in ("-s", "--no-patch"):
            rest.append("-s")
        elif a in ("--no-decorate", "--no-color", "HEAD"):
            pass
        else:
            return False
        i += 1
    if sub == "show" and "-s" not in rest:
        return False
    return one and (fmt in HEX_FORMATS or fmt == "oneline")


UNKNOWN = "\x00?"


def places(cmds):
    here, out = None, []
    for toks, _op in cmds:
        t = strip_env(toks)
        g = git_sub(toks)
        if here == UNKNOWN:
            out.append(UNKNOWN)
        elif g and g[2] is not None:
            out.append(g[2] if path.isabs(g[2]) or here is None else path.join(here, g[2]))
        else:
            out.append(here)
        if t and t[0] in ("cd", "pushd", "popd"):
            args = [a for a in t[1:] if a != "--"]
            if t[0] == "popd" or len(args) != 1 or args[0].startswith(("-", "~")) or "$" in args[0]:
                here = UNKNOWN
            elif here != UNKNOWN:
                here = args[0] if path.isabs(args[0]) or here is None else path.join(here, args[0])
    return out


def same_place(where, cwd, physical=False):
    if where == UNKNOWN:
        return False
    if where is None or where in (".", "./"):
        return True
    if not cwd:
        return False
    if physical:
        raise ValueError("filesystem resolution is not part of the offline grammar")
    if ".." in where.replace("\\", "/").split("/"):
        return False
    return path.normpath(path.join(cwd, where)) == path.normpath(cwd)


GIT_SAFE_ENV = {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
                "GIT_COMMITTER_DATE", "GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "GIT_PAGER", "GIT_TERMINAL_PROMPT", "GIT_SSH",
                "GIT_SSH_COMMAND", "GIT_ASKPASS", "GIT_OPTIONAL_LOCKS", "GIT_MERGE_AUTOEDIT", "GIT_PROGRESS_DELAY",
                "GIT_TRACE", "GIT_FLUSH", "GIT_ADVICE"}
EXPANDABLE = {"commit": ("-m", "--message", "-F", "--file"), "merge": ("-m", "--message", "-F", "--file"),
              "pr-create": ("-t", "--title", "-b", "--body", "-F", "--body-file"), "pr-ready": ()}


def env_of(argv):
    return [tuple(a.split("=", 1)) for a in argv[:len(argv) - len(strip_env(argv))]]


def expands_control(c, kind):
    argv, exp = c["argv"], c["exp"]
    lead = len(argv) - len(strip_env(argv))
    allowed = EXPANDABLE.get(kind, ())
    for k in range(len(argv)):
        expands, splits = exp[k]
        if not expands:
            continue
        prev = argv[k - 1] if k > lead else None
        word = argv[k]
        value = prev in allowed or any(word.startswith(o + "=") or (len(o) == 2 and word.startswith(o) and len(word) > 2)
                                       for o in allowed)
        if value and not splits:
            continue
        return True
    return False


PUSH_SHORT = set("46dfnquv")
PUSH_WITH_ARG = {"-o", "--push-option", "--receive-pack", "--exec", "--repo"}
NO_COMMIT = {"commit": {"--dry-run", "-h", "--help"},
             "merge": {"--abort", "--quit", "--ff-only", "--squash", "--no-commit", "-h", "--help"}}
GH_WITH_ARG = {"-R", "--repo", "-H", "--head", "-B", "--base", "-t", "--title", "-b", "--body", "-F", "--body-file",
               "-a", "--assignee", "-l", "--label", "-m", "--milestone", "-p", "--project", "-r", "--reviewer", "-T", "--template"}


def push_args(args):
    dry = delete = False
    pos, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos.extend(args[i + 1:])
            break
        if a in PUSH_WITH_ARG:
            i += 2
            continue
        if a.startswith("--"):
            if a not in {"--dry-run", "--delete", "--force", "--force-with-lease", "--atomic", "--quiet", "--verbose", "--porcelain", "--tags", "--all", "--follow-tags", "--no-verify", "--set-upstream"}:
                return None
            dry = dry or a == "--dry-run"
            delete = delete or a == "--delete"
        elif a.startswith("-") and len(a) > 1:
            if not set(a[1:]) <= PUSH_SHORT:
                return None
            dry, delete = dry or "n" in a, delete or "d" in a
        else:
            pos.append(a)
        i += 1
    return dry, delete, (pos[0] if pos else None), pos[1:]


GH_SHORT_WITH_ARG = {"-R", "-H", "-B", "-t", "-b", "-F", "-a", "-l", "-m", "-p", "-r", "-T"}


def gh_args(args):
    out, i = {"repo": None, "head": None, "selector": None, "draft": False, "unparsed": False}, 0
    while i < len(args):
        a = args[i]
        key, val, step = a, None, 1
        if a.startswith("--") and "=" in a:
            key, val = a.split("=", 1)
        elif a in GH_WITH_ARG:
            val, step = (args[i + 1] if i + 1 < len(args) else None), 2
        elif a.startswith("-") and not a.startswith("--") and len(a) > 2:
            if a[:2] in GH_SHORT_WITH_ARG:
                key, val = a[:2], a[2:]
            elif set(a[1:]) <= {"d", "f", "w"}:
                out["draft"] = out["draft"] or "d" in a
                i += 1
                continue
            else:
                out["unparsed"] = True
                i += 1
                continue
        if key in ("-R", "--repo"):
            out["repo"] = val
        elif key in ("-H", "--head"):
            out["head"] = val
        elif a in ("-d", "--draft"):
            out["draft"] = True
        elif not a.startswith("-") and out["selector"] is None:
            out["selector"] = a
        i += step
    return out


def pr_printed(cmd, out, cwd, physical=False):
    s = structure(cmd)
    ops = [o for o in operations(cmd, cwd, physical) or [] if o["op"] in ("pr-create", "pr-ready", "unresolved")]
    if not s or len(ops) != 1 or not ops[0]["op"].startswith("pr-") or not ops[0]["certain"]:
        return None
    if not quiet_companions(s, ops[0]["index"]):
        return None
    refs = {(m.group(1).lower(), m.group(2).lower(), int(m.group(3))) for m in PR_URL.finditer(out or "")}
    refs |= {(m.group(1).lower(), m.group(2).lower(), int(m.group(3))) for m in PR_READY.finditer(out or "")}
    if len(refs) != 1:
        return None
    m = PR_URL.search(out or "") or PR_READY.search(out or "")
    return m.group(1), m.group(2), int(m.group(3))


def operations(cmd, cwd=None, physical=False):
    s = structure(cmd)
    if s is None:
        return None
    cmds = [(c["argv"], c["op"]) for c in s]
    out, where = [], places(cmds)
    env_repo = {}
    for i, c in enumerate(s):
        pre = c["argv"][:len(c["argv"]) - len(strip_env(c["argv"]))]
        for a in pre:
            if a.startswith("GH_REPO="):
                env_repo[i] = a.split("=", 1)[1]
        if c["argv"] and c["argv"][0] in ("export", "declare", "typeset", "set") and any("GH_REPO" in a for a in c["argv"]):
            for j in range(i + 1, len(s)):
                env_repo.setdefault(j, "?")
    git_env_after = None
    for i, c in enumerate(s):
        if c["argv"] and c["argv"][0] in ("export", "declare", "typeset", "set", "source", ".", "eval") and \
                any(a.startswith("GIT_") and a.split("=", 1)[0] not in GIT_SAFE_ENV for a in c["argv"][1:]) | (c["argv"][0] in ("source", ".", "eval")):
            git_env_after = i if git_env_after is None else git_env_after
    for i, c in enumerate(s):
        toks, op = c["argv"], c["op"]
        base = {"index": i, "where": where[i], "same_repo": same_place(where[i], cwd, physical), "certain": c["certain"],
                "creates": False, "dry_run": False, "delete": False, "remote": None, "refspecs": [], "head_print": False,
                "gh": None}
        g = git_sub(toks)
        if g and g[0] == "":
            out.append({**base, "op": "unresolved", "same_repo": False})
            continue
        if g and g[0] in OPS and (expands_control(c, g[0]) or (git_env_after is not None and i > git_env_after)
                                  or any(n.startswith("GIT_") and n not in GIT_SAFE_ENV for n, _ in env_of(toks))):
            out.append({**base, "op": "unresolved", "same_repo": False})
            continue
        if g and g[0] in OPS:
            nxt = s[i + 1]["argv"] if i + 1 < len(s) else None
            head_print = op == "&&" and nxt is not None and prints_head(nxt) and where[i + 1] == where[i]
            rec = {**base, "op": g[0], "head_print": head_print}
            if g[0] == "push":
                pa = push_args(g[1])
                if pa is None:
                    out.append({**rec, "op": "unresolved", "same_repo": False})
                    continue
                rec.update(dry_run=pa[0], delete=pa[1], remote=pa[2], refspecs=pa[3])
            else:
                rec["creates"] = not (set(g[1]) & NO_COMMIT[g[0]])
            out.append(rec)
            continue
        p = gh_sub(toks)
        if p and expands_control(c, p[0]):
            out.append({**base, "op": "unresolved", "same_repo": False})
            continue
        if p:
            gh = gh_args(p[1])
            if gh["repo"] is None and i in env_repo:
                gh["repo"] = env_repo[i]
            out.append({**base, "op": p[0], "gh": gh})
    return out


def resolve(abbrev, known):
    hits = [h for h in known if h.startswith(abbrev)]
    return hits[0] if len(hits) == 1 else None


QUIET_GIT = {"add", "commit", "merge", "push"}


HEX = frozenset("0123456789abcdef")
WORDCH = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")


def is_hex(t, lo=7, hi=40):
    return lo <= len(t) <= hi and set(t) <= HEX


def bracket_abbrevs(out):
    found = []
    for line in (out or "").splitlines():
        if line.startswith("["):
            j = line.find("]")
            parts = line[1:j].rsplit(" ", 1) if j > 0 else []
            if len(parts) == 2 and is_hex(parts[1]):
                found.append(parts[1])
    return found


def hex_lines(out):
    found = []
    for line in (out or "").splitlines():
        k = 0
        while k < len(line) and line[k] in HEX:
            k += 1
        if 7 <= k <= 40 and (k == len(line) or line[k] not in WORDCH):
            found.append(line[:k])
    return found


def push_lines(out):
    found = []
    for line in (out or "").splitlines():
        t = line.split()
        flag = ""
        if t and t[0] == "+":
            flag, t = "+", t[1:]
        elif t and t[0].startswith("+") and len(t[0]) > 1:
            flag, t = "+", [t[0][1:]] + t[1:]
        if len(t) < 4 or t[2] != "->":
            continue
        sep = "..." if "..." in t[0] else ".."
        rng = t[0].split(sep)
        if len(rng) == 2 and is_hex(rng[0]) and is_hex(rng[1]):
            found.append((flag, rng[0], rng[1], t[1], t[3]))
    return found


def quiet_companions(s, head_print=None):
    for i, c in enumerate(s):
        if i == head_print:
            continue
        w = strip_env(c["argv"])
        if not w:
            continue
        if w[0] in ("cd", "pushd", "true", ":") or c.get("quiet"):
            continue
        g = git_sub(c["argv"])
        if not g or g[0] not in QUIET_GIT:
            return False
    return True


def printed_commit(cmd, out, cwd, physical=False):
    s = structure(cmd)
    if not s:
        return None
    ops = [o for o in operations(cmd, cwd, physical) or [] if o["op"] in ("commit", "merge", "unresolved")]
    if len(ops) != 1 or ops[0]["op"] == "unresolved":
        return None
    o = ops[0]
    head = o["index"] + 1 if o["head_print"] else None
    captured_head = head is not None and s[head]["certain"] and s[head]["stdout"] == "out"
    known_head_routing = head is not None and all(s[head][fd] in ("out", "file", "closed") for fd in ("stdout", "stderr"))
    if not quiet_companions(s, head if known_head_routing else None):
        return None
    if not (o["certain"] and o["creates"] and o["same_repo"]):
        return None
    if o["op"] == "merge":
        if (s[o["index"]]["stdout"] != "out" or not captured_head
                or not any(line.startswith("Merge made by ") for line in out.splitlines())):
            return None
    brackets = bracket_abbrevs(out) if o["op"] == "commit" and s[o["index"]]["stdout"] == "out" else []
    if len(brackets) > 1:
        return None
    if captured_head:
        found = set(hex_lines(out))
        if len(found) != 1:
            return None
        sha = found.pop()
        if brackets and not sha.startswith(brackets[0]):
            return None
        return sha, "head printed"
    return (brackets[0], "commit line") if brackets else None


def produced_by(cmd, out, known, cwd):
    p = printed_commit(cmd, out, cwd)
    return resolve(p[0], known) if p else None


def pushes(cmd, out, cwd, physical=False):
    s = structure(cmd)
    if not s:
        return []
    ops = [o for o in operations(cmd, cwd, physical) or [] if o["op"] in ("push", "unresolved")]
    creation = [o for o in operations(cmd, cwd, physical) or [] if o["op"] in ("commit", "merge")]
    head = None
    if len(creation) == 1:
        c = creation[0]
        if c["certain"] and c["creates"] and c["same_repo"] and c["head_print"] and s[c["index"] + 1]["stdout"] == "out":
            head = c["index"] + 1
    if len(ops) != 1 or ops[0]["op"] != "push" or not quiet_companions(s, head):
        return []
    o = ops[0]
    if not (o["certain"] and o["same_repo"]) or o["dry_run"] or o["delete"]:
        return []
    if s[o["index"]]["stderr"] != "out":
        return []
    return [{"dst": dst.replace("refs/heads/", ""), "old": old, "new": new, "forced": flag == "+", "remote": o["remote"]}
            for flag, old, new, _src, dst in push_lines(out)]


def published_by(cmd, out, known, head_ref, cwd):
    return {h for p in pushes(cmd, out, cwd) if p["dst"] == head_ref for h in [resolve(p["new"], known)] if h}
