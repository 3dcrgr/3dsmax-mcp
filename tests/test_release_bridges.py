import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import install
spec = importlib.util.spec_from_file_location("release_builder", ROOT / "installer" / "build_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "native" / "bin"
        self.bin.mkdir(parents=True)
        self.current, self.old = b"reviewed fixed bridge", b"known retired bridge"
        self.entry = {"sha256": hashlib.sha256(self.current).hexdigest(),
                      "superseded_sha256": [hashlib.sha256(self.old).hexdigest()]}
        (self.bin / "mcp_bridge_2027.gup").write_bytes(self.current)
        (self.bin / "bridge_manifest.json").write_text(json.dumps({"2027": self.entry}), encoding="utf-8")
        for module, attr, value in ((release, "ROOT", self.root), (install, "ROOT", self.root),
                                   (install, "GUP_SRCS", {2027: self.bin / "mcp_bridge_2027.gup"})):
            patch = mock.patch.object(module, attr, value)
            patch.start(); self.addCleanup(patch.stop)

    def wheel(self, bridge=None):
        path = self.root / "test.whl"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("maxmcp/native/bin/bridge_manifest.json", (self.bin / "bridge_manifest.json").read_bytes())
            z.writestr("maxmcp/native/bin/mcp_bridge_2027.gup", bridge or self.current)
        return path

    def test_current_wheel_and_sources_pass(self):
        release.validate_wheel_bridges(self.wheel())
        install.validate_bridge_sources()

    def test_cached_old_wheel_fails_before_stage_changes(self):
        stage = self.root / "stage"
        stage.mkdir(); (stage / "keep").write_text("preserve")
        with self.assertRaisesRegex(ValueError, "Stale wheel"):
            release.stage(self.root / "runtime", self.wheel(self.old), stage)
        self.assertTrue((stage / "keep").exists())

    def test_source_binary_must_match_reviewed_manifest(self):
        (self.bin / "mcp_bridge_2027.gup").write_bytes(self.old)
        with self.assertRaisesRegex(ValueError, "reviewed"):
            install.validate_bridge_sources()

    def test_missing_reviewed_source_fails_before_bundle_changes(self):
        (self.bin / "mcp_bridge_2027.gup").unlink()
        dst = self.root / "bundle"
        with self.assertRaisesRegex(ValueError, "Missing reviewed bridge"):
            install.stage_bundle(dst)
        self.assertFalse(dst.exists())

    def test_unknown_installed_bridge_is_not_replaceable(self):
        dst = self.root / "installed"
        target = dst / "Contents" / "bin" / "mcp_bridge_2027.gup"
        target.parent.mkdir(parents=True)
        for payload, conflict in ((self.old, False), (self.current, False), (b"unrecognized newer bridge", True)):
            target.write_bytes(payload)
            self.assertEqual(bool(install.bridge_replacement_conflicts(dst)), conflict)
            self.assertEqual(target.read_bytes(), payload)
