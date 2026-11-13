import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "collectors" / "examples"
sys.path[:0] = [str(EXAMPLES), str(ROOT / "collectors")]


def example(name):
    spec = importlib.util.spec_from_file_location("example_" + name, EXAMPLES / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


machine, mac, rig, vm = (example(n) for n in ("machine", "mac", "rig", "vm"))

HOST_KEYS = {"cpu", "mem_used_gb", "mem_total_gb", "swap_used_gb", "load", "temp_c", "fan_rpm", "uptime_h", "disk_pct", "net_fail_min"}
VM_KEYS = {"mem_used_gb", "mem_total_gb", "load", "cpus", "disk_pct", "orch", "orch_token_expires", "claude_sessions",
           "claude_working", "codex_sessions", "codex_working"}


def run(name):
    out = subprocess.run([sys.executable, "-I", str(EXAMPLES / (name + ".py"))], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout)


class Shapes(unittest.TestCase):
    def test_rig_reports_this_machine_as_the_host_and_nothing_it_cannot_see(self):
        out = run("rig")
        self.assertAlmostEqual(out["collected"], time.time(), delta=60)
        self.assertEqual(set(out["host"]), HOST_KEYS)
        self.assertTrue(out["vm_skipped"])
        self.assertNotIn("vms", out)
        self.assertNotIn("ci", out)
        for unknown in ("temp_c", "fan_rpm", "swap_used_gb", "net_fail_min"):
            self.assertIsNone(out["host"][unknown])

    def test_vm_reports_counters_and_leaves_the_orchestrator_unknown(self):
        out = run("vm")
        self.assertEqual(set(out["vm"]), VM_KEYS)
        self.assertNotIn("agents", out)
        self.assertEqual(out["agents_error"], vm.NO_AGENTS)
        for unknown in VM_KEYS - {"mem_used_gb", "mem_total_gb", "load", "cpus", "disk_pct"}:
            self.assertIsNone(out["vm"][unknown])

    def test_measurements_are_in_range_or_unknown(self):
        h = machine.host()
        self.assertTrue(h["cpu"] is None or 0 <= h["cpu"] <= 100)
        self.assertTrue(h["disk_pct"] is None or 0 <= h["disk_pct"] <= 100)
        self.assertTrue(h["load"] is None or all(isinstance(x, str) for x in h["load"]))
        if h["mem_used_gb"] is not None:
            self.assertLessEqual(h["mem_used_gb"], h["mem_total_gb"] + 1)


class Mac(unittest.TestCase):
    def test_a_budget_is_kept_with_its_fetch_time(self):
        budget = {"core": 4000, "core_limit": 5000, "core_reset": 1, "graphql": 4900}
        out = mac.build(now=lambda: 100.0, budget=lambda: budget)
        self.assertEqual(out["github"], {"value": budget, "fetched": 100.0})
        self.assertEqual(out["collected"], 100.0)

    def test_a_failed_budget_read_is_an_error_not_a_zero(self):
        def fail():
            raise FileNotFoundError("gh")
        out = mac.build(now=lambda: 100.0, budget=fail)
        self.assertEqual(out["github"], {"value": None, "fetched": None, "error": "gh"})

    def test_parts_it_cannot_read_are_named(self):
        out = mac.build(now=lambda: 100.0, budget=lambda: {})
        self.assertEqual(set(out["errors"]), {"ios_ci", "codex", "agents"})
        for missing in ("ios_ci", "ios_lane", "codex", "agents", "services"):
            self.assertNotIn(missing, out)


class Builders(unittest.TestCase):
    def test_rig_and_vm_keep_the_clock_they_are_given(self):
        self.assertEqual(rig.build(now=lambda: 5.0, read=lambda: {})["collected"], 5.0)
        self.assertEqual(vm.build(now=lambda: 6.0, read=lambda: {})["collected"], 6.0)


if __name__ == "__main__":
    unittest.main()
