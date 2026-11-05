import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 10000
PATTERNS = (
    r"\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b",
    r"/(Users|home)/[A-Za-z0-9._-]+/",
    r"\b[A-Za-z0-9-]+\.ts\.net\b",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
)
UNSUPPORTED_SUFFIXES = {
    ".7z", ".a", ".app", ".bin", ".bz2", ".db", ".dmg", ".doc",
    ".docx", ".dylib", ".exe", ".gif", ".gz", ".icns", ".ico",
    ".jar", ".jpeg", ".jpg", ".key", ".mp3", ".mp4", ".o", ".pdf",
    ".pkg", ".png", ".ppt", ".pptx", ".rar", ".so", ".sqlite",
    ".sqlite3", ".tar", ".tgz", ".tif", ".tiff", ".wasm", ".webp",
    ".xls", ".xlsx", ".xz", ".zip", ".zst",
}


class Refused(Exception):
    pass


def git(target, *args):
    result = subprocess.run(
        ["git", "-C", str(target), *args], stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, check=False, timeout=30,
    )
    if result.returncode:
        raise Refused("Git input could not be read")
    return result.stdout


def arguments(args):
    if args == ["--staged"]:
        return "staged", Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
    if len(args) == 2 and args[0] == "--tree":
        return "tree", Path(args[1]).resolve()
    if not args:
        return "repo", Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
    raise Refused("usage: scripts/leak-check.sh [--staged | --tree DIR]")


def patterns():
    values = list(PATTERNS)
    supplied = os.environ.get("FLIGHTOPS_DENYLIST")
    path = Path(supplied) if supplied else ROOT / ".leak-denylist"
    if supplied or path.exists():
        if path.stat().st_size > MAX_FILE_BYTES:
            raise Refused("private pattern configuration exceeds scan limits")
        values.extend(
            line for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        )
    return [re.compile(value, re.IGNORECASE) for value in values]


def index_entries(target, mode):
    changed = None
    if mode == "staged":
        changed = set(git(target, "diff", "--cached", "--name-only", "--diff-filter=ACMRTUXB", "-z").split(b"\0"))
    for entry in git(target, "ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        permissions, oid, stage = metadata.split()
        if changed is not None and name not in changed:
            continue
        if stage != b"0" or permissions not in (b"100644", b"100755"):
            raise Refused("unsupported Git entry requires a separate privacy review")
        yield name.decode("utf-8"), oid.decode("ascii")


def tree_entries(target):
    if not target.is_dir():
        raise Refused("scan target is not a directory")
    def unreadable(error):
        raise Refused("directory inventory could not be read")

    for directory, dirs, files in os.walk(target, followlinks=False, onerror=unreadable):
        dirs[:] = sorted(name for name in dirs if name != ".git")
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise Refused("symbolic links require a separate privacy review")
        for name in sorted(files):
            if name == ".git":
                continue
            path = Path(directory) / name
            yield str(path.relative_to(target)), path


def read_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise Refused("nonregular files require a separate privacy review")
        data = stream.read(MAX_FILE_BYTES + 1)
    return data


def matches(text, rules):
    return any(rule.search(text) for rule in rules)


def snapshot(mode, target, destination, rules):
    entries = tree_entries(target) if mode == "tree" else index_entries(target, mode)
    total = 0
    names = []
    for number, (name, source) in enumerate(entries, start=1):
        if number > MAX_FILES:
            raise Refused("file inventory exceeds scan limits")
        if matches(name, rules):
            raise Refused("a file name matches a private pattern")
        names.append(name)
        total += len(name.encode("utf-8"))
        if Path(name).suffix.lower() in UNSUPPORTED_SUFFIXES:
            raise Refused("unsupported binary or archive format requires a separate privacy review")
        if mode == "staged":
            size = int(git(target, "cat-file", "-s", source))
            if size > MAX_FILE_BYTES:
                raise Refused("file content exceeds scan limits")
            data = git(target, "cat-file", "blob", source)
        else:
            path = source if mode == "tree" else target / name
            if path.is_symlink():
                raise Refused("symbolic links require a separate privacy review")
            data = read_file(path)
        total += len(data)
        if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise Refused("file content exceeds scan limits")
        text = data.decode("utf-8")
        if any(ord(character) < 32 and character not in "\t\r\n" for character in text):
            raise Refused("unsupported binary content requires a separate privacy review")
        if matches(text, rules):
            raise Refused("file content matches a private pattern")
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return names


def scan(args):
    mode, target = arguments(args)
    executable = shutil.which("gitleaks")
    if executable is None:
        raise Refused("gitleaks is required; install it before publication")
    rules = patterns()
    with tempfile.TemporaryDirectory(prefix="flightops-privacy-") as temporary:
        workspace = Path(temporary)
        contents = workspace / "contents"
        contents.mkdir()
        names = snapshot(mode, target, contents, rules)
        filenames = workspace / "paths"
        filenames.mkdir()
        (filenames / "filenames.txt").write_text("\n".join(names), encoding="utf-8")
        ignore = workspace / "empty.ignore"
        ignore.touch()
        command = [
            executable, "dir", "--config", str(ROOT / ".gitleaks.toml"),
            "--redact", "--no-banner", "--no-color", "--log-level", "fatal",
            "--gitleaks-ignore-path", str(ignore), "--timeout", "60", str(contents),
        ]
        for source in (contents, filenames):
            command[-1] = str(source)
            if subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=70, check=False).returncode:
                raise Refused("gitleaks found a secret or could not complete")
        if mode == "repo":
            command[1] = "git"
            command[-1] = str(target)
            if subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=70, check=False).returncode:
                raise Refused("gitleaks history scan found a secret or could not complete")


def main():
    try:
        scan(sys.argv[1:])
    except Refused as problem:
        print(f"leak-check: FAILED ({problem})", file=sys.stderr)
        return 1
    except (OSError, ValueError, UnicodeError, re.error, subprocess.SubprocessError):
        print("leak-check: FAILED (scan input or tool could not be processed)", file=sys.stderr)
        return 1
    print("leak-check: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
