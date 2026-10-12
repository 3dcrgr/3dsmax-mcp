import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from maxmcp import max_client
from maxmcp.max_client import MaxClient


class RegistryCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "instances").mkdir()
        self.client = MaxClient(transport="pipe")
        self.data = {"pid": 4242, "instance_id": "pid-4242", "pipe": r"\\.\pipe\3dsmax-mcp-pid-4242"}
        self.path = self.root / "instances" / "pid-4242.json"
        self.path.write_text(json.dumps(self.data), encoding="utf-8")
        for target, value in (("_config_dir", mock.Mock(return_value=self.root)),
                              ("_probe_pipe_available", mock.Mock(return_value=False))):
            p = mock.patch.object(self.client, target, value)
            p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(max_client, "process_gone", return_value=True)
        self.gone = p.start(); self.addCleanup(p.stop)

    def test_dead_owner_and_missing_pipe_removes_only_instance_record(self):
        claim = self.root / "active_instance.json"
        claim.write_text(json.dumps(self.data), encoding="utf-8")
        self.assertEqual(self.client._live_instances(), [])
        self.assertFalse(self.path.exists())
        self.assertTrue(claim.exists())

    def test_answering_or_busy_pipe_is_preserved_even_if_pid_looks_dead(self):
        self.client._probe_pipe_available.return_value = True
        self.assertEqual(len(self.client._live_instances()), 1)
        self.assertTrue(self.path.exists())
        self.gone.assert_not_called()

    def test_live_or_unknown_owner_with_missing_pipe_is_preserved(self):
        self.gone.return_value = False
        self.assertEqual(self.client._live_instances(), [])
        self.assertTrue(self.path.exists())

    def test_pipe_becoming_available_during_cleanup_is_preserved(self):
        self.client._probe_pipe_available.side_effect = [False, True]
        self.client._live_instances()
        self.assertTrue(self.path.exists())

    def test_owner_becoming_live_during_cleanup_is_preserved(self):
        self.gone.side_effect = [True, False]
        self.client._live_instances()
        self.assertTrue(self.path.exists())

    def test_refreshed_record_is_preserved(self):
        def refresh(pid):
            self.path.write_text(json.dumps({**self.data, "max_version": 2027}), encoding="utf-8")
            return True
        self.gone.side_effect = refresh
        self.client._live_instances()
        self.assertTrue(self.path.exists())

    def test_custom_or_mismatched_record_is_preserved(self):
        for changed in ({"pid": True}, {"pid": 5151}, {"instance_id": "custom"}, {"pipe": r"\\.\pipe\custom"}):
            self.path.write_text(json.dumps({**self.data, **changed}), encoding="utf-8")
            self.client._live_instances()
            self.assertTrue(self.path.exists())

    def test_unlink_permission_failure_does_not_break_listing(self):
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("read-only")):
            self.assertEqual(self.client._live_instances(), [])
        self.assertTrue(self.path.exists())


if __name__ == "__main__":
    unittest.main()
