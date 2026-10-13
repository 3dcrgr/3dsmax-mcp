import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from maxmcp.diagnostics import stackdump


class ShutdownClassificationTests(unittest.TestCase):
    def capture(self, frames):
        return {"threads": [{"tid": 42, "is_main": True, "frames": frames, "windows": []}]}

    def test_shutdown_join_is_not_active_request(self):
        frames = [{"module": "ntdll", "symbol": "NtWaitForSingleObject"},
                  {"module": "KERNELBASE", "symbol": "WaitForSingleObjectEx"},
                  {"module": "MSVCP140", "symbol": "Thrd_join+0x21"},
                  {"module": "mcp_bridge_2027", "offset": 0x1426f},
                  {"module": "mcp_bridge_2027", "offset": 0xcc6a},
                  {"module": "gup", "symbol": "GUPManager::~GUPManager+0x13e"}]
        findings = stackdump.summarize(self.capture(frames))["findings"]
        kinds = {f["kind"] for f in findings}
        self.assertIn("mcp_shutdown_join", kinds)
        self.assertNotIn("mcp_bridge_call", kinds)
        self.assertIn("symbols are needed", next(f["message"] for f in findings if f["kind"] == "mcp_shutdown_join"))

    def test_bridge_frame_without_teardown_does_not_claim_request(self):
        findings = stackdump.summarize(self.capture([{"module": "mcp_bridge_2027", "offset": 42}]))["findings"]
        finding = next(f for f in findings if f["kind"] == "mcp_bridge_call")
        self.assertIn("does not establish an active request", finding["message"])
        self.assertNotIn("mcp_shutdown_join", {f["kind"] for f in findings})

    def test_wait_with_gup_but_no_join_symbol_is_not_proven_shutdown_join(self):
        findings = stackdump.summarize(self.capture([{"module": "ntdll", "symbol": "NtWaitForSingleObject"},
                                                    {"module": "mcp_bridge_2027"}, {"module": "gup"}]))["findings"]
        self.assertNotIn("mcp_shutdown_join", {f["kind"] for f in findings})
        self.assertNotIn("mcp_bridge_call", {f["kind"] for f in findings})
        self.assertIn("mcp_teardown_wait", {f["kind"] for f in findings})
