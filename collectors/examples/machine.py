import os
import re
import shutil
import subprocess
import sys
import time

TIMEOUT = 5


def command(*args):
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout


def meminfo():
    try:
        with open("/proc/meminfo") as f:
            rows = dict(line.split(":", 1) for line in f if ":" in line)
        total = int(rows["MemTotal"].split()[0]) * 1024
        available = int(rows["MemAvailable"].split()[0]) * 1024
    except (OSError, KeyError, ValueError, IndexError):
        return None
    return total, total - available


def darwin_memory():
    size, stat = command("sysctl", "-n", "hw.memsize"), command("vm_stat")
    if size is None or stat is None:
        return None
    page = re.search(r"page size of (\d+) bytes", stat)
    pages = {k.strip(): int(v.strip().rstrip(".")) for k, v in re.findall(r"^([^:\n]+):\s+(\d+\.?)$", stat, re.M)}
    try:
        used = sum(pages[k] for k in ("Pages active", "Pages wired down", "Pages occupied by compressor")) * int(page.group(1))
        return int(size.strip()), used
    except (KeyError, AttributeError, ValueError):
        return None


def memory():
    found = meminfo() if sys.platform.startswith("linux") else darwin_memory() if sys.platform == "darwin" else None
    if found is None:
        return None, None
    total, used = found
    return round(used / 2**30, 1), round(total / 2**30)


def proc_cpu():
    try:
        with open("/proc/stat") as f:
            fields = [int(x) for x in f.readline().split()[1:]]
    except (OSError, ValueError):
        return None
    return sum(fields), fields[3] + (fields[4] if len(fields) > 4 else 0)


def cpu_percent(pause=0.5):
    if sys.platform.startswith("linux"):
        first = proc_cpu()
        time.sleep(pause)
        second = proc_cpu()
        if first is None or second is None or second[0] <= first[0]:
            return None
        busy = 1 - (second[1] - first[1]) / (second[0] - first[0])
        return max(0, min(100, round(busy * 100)))
    out = command("ps", "-A", "-o", "%cpu=")
    cpus = os.cpu_count()
    if out is None or not cpus:
        return None
    try:
        total = sum(float(x) for x in out.split())
    except ValueError:
        return None
    return max(0, min(100, round(total / cpus)))


def uptime_hours():
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]) // 3600)
    except (OSError, ValueError, IndexError):
        pass
    out = command("sysctl", "-n", "kern.boottime")
    found = re.search(r"sec = (\d+)", out or "")
    return int((time.time() - int(found.group(1))) // 3600) if found else None


def load():
    try:
        return ["%.2f" % x for x in os.getloadavg()]
    except OSError:
        return None


def disk_percent(path="/"):
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return round(usage.used * 100 / usage.total) if usage.total else None


def host():
    used, total = memory()
    return {"cpu": cpu_percent(), "mem_used_gb": used, "mem_total_gb": total, "swap_used_gb": None, "load": load(),
            "temp_c": None, "fan_rpm": None, "uptime_h": uptime_hours(), "disk_pct": disk_percent(), "net_fail_min": None}


def counters():
    used, total = memory()
    return {"mem_used_gb": used, "mem_total_gb": total, "load": load(), "cpus": os.cpu_count(), "disk_pct": disk_percent(),
            "orch": None, "orch_token_expires": None, "claude_sessions": None, "claude_working": None,
            "codex_sessions": None, "codex_working": None}
