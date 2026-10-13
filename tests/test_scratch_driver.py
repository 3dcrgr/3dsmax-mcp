import json
import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path

HELPER = Path(os.environ.get("SCRATCH_HELPER_PATH", "livetests-2027/scratch_max.py"))


@unittest.skipUnless(HELPER.exists(), "shared scratch helper unavailable")
class ScratchDriverTests(unittest.TestCase):
    def run_driver(self, result, mode="mxs", wrong_pid=False):
        tree = ast.parse(HELPER.read_text(encoding="utf-8-sig"))
        pid = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "SCRATCH_PID" for t in n.targets))
        source = f'''
import json, runpy, sys, types
result = json.loads({json.dumps(json.dumps(result))})
class Client:
    def resolve_target(self): return {{"target_pid": {0 if wrong_pid else pid}}}
    def send_command(self, script, **kwargs):
        assert "NOT_SCRATCH_MAX" in script
        return {{"result": result}}
client_module = types.ModuleType("maxmcp.max_client")
client_module.MaxClient = Client
sys.modules["maxmcp.max_client"] = client_module
server = types.ModuleType("maxmcp.server")
server.client = Client()
sys.modules["maxmcp.server"] = server
tool = types.ModuleType("maxmcp.tools.offline_fixture")
tool.probe = lambda **kwargs: result
sys.modules["maxmcp.tools.offline_fixture"] = tool
sys.argv = ["scratch_max.py"] + {repr(['mxs', '-e', 'true'] if mode == 'mxs' else ['tool', 'offline_fixture', 'probe'])}
runpy.run_path({str(HELPER)!r}, run_name="__main__")
'''
        return subprocess.run([sys.executable, "-c", source], capture_output=True,
                              env={**os.environ, "PYTHONIOENCODING": "cp1252:strict"}, timeout=10)

    def test_unicode_success_prints_utf8(self):
        result = self.run_driver("עברית € ✓")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("עברית € ✓", result.stdout.decode("utf-8"))

    def test_native_marker_is_failure(self):
        self.assertEqual(self.run_driver("__MCP_MS_ERR__: bad syntax").returncode, 1)

    def test_structured_failures_have_nonzero_exit(self):
        for failure in ({"ok": False}, {"success": False}, {"status": "failed"}, {"error": "bad"}):
            for mode in ("mxs", "tool"):
                with self.subTest(failure=failure, mode=mode):
                    self.assertEqual(self.run_driver(failure, mode).returncode, 1)

    def test_pid_mismatch_retains_exit_two(self):
        self.assertEqual(self.run_driver("ok", wrong_pid=True).returncode, 2)

    def test_message_only_is_not_failure(self):
        self.assertEqual(self.run_driver({"message": "valid"}, "tool").returncode, 0)
