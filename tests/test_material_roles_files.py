"""material_roles must never look complete when a file-bearing map yields no path (issue #5)."""

import sys
import unittest
from pathlib import Path
from unittest import mock

# The embedded runtime ignores PYTHONPATH (python312._pth); prefer the checkout
# over an installed maxmcp.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from maxmcp.helpers import material_roles as impl  # noqa: E402

assert Path(impl.__file__).resolve().parent.parent.parent == REPO_ROOT, impl.__file__


def _graph(*maps: dict) -> dict:
    """One Autodesk_Material root with one slot per given map node."""
    nodes = [{"id": "n0", "kind": "material", "class": "Autodesk Material", "name": "Wood_Revit", "handle": "11"}]
    edges = []
    for i, node in enumerate(maps, start=1):
        node = {"id": f"n{i}", "kind": "texture", "handle": str(100 + i), **node}
        nodes.append(node)
        edges.append({"parentId": "n0", "nodeId": node["id"], "slot": node.pop("slot"),
                      "slotKey": f"map:{i}", "kind": "map", "inputType": 2, "role": "map", "aliases": []})
    return {"graphVersion": 2, "query": "Wood_Revit", "owner": "Floor",
            "root": {"id": "n0", "class": "Autodesk Material", "handle": "11", "rendererProfile": "generic"},
            "nodes": nodes, "edges": edges, "issues": [], "warnings": [], "complete": True,
            "truncated": {"nodesOmitted": 0, "edgesOmitted": 0, "depthLimited": []},
            "fileManifest": [], "hints": {}}


def _file(path: str, param: str = "fileName", exists: bool = True) -> dict:
    return {"path": path, "param": param, "exists": exists, "bytes": 5000}


def _codes(result: dict) -> list[str]:
    return [w["code"] for w in result["warnings"]]


class UnreadableFileWarningTests(unittest.TestCase):
    def test_autodesk_bitmap_without_path_is_flagged_and_incomplete(self):
        result = impl.roles_from_payload(_graph(
            {"class": "Autodesk Bitmap", "name": "Generic_Image", "slot": "Generic_Image"}))
        self.assertFalse(result["complete"])
        self.assertEqual(_codes(result), ["FILE_PATH_UNREADABLE"])
        warning = result["warnings"][0]
        self.assertEqual(warning["nodeId"], "n1")
        self.assertIn("Generic_Image", warning["message"])
        self.assertIn("Autodesk Bitmap", warning["message"])
        # The slot is still reported, without a file.
        self.assertEqual([(r["slot"], r["file"]) for r in result["roles"]], [("Generic_Image", None)])

    def test_other_file_map_classes_are_flagged(self):
        for cls in ("Bitmap", "BitmapTexture", "VRayBitmap", "VRayHDRI", "CoronaBitmap", "ai_image"):
            with self.subTest(cls=cls):
                result = impl.roles_from_payload(_graph({"class": cls, "name": "Map #1", "slot": "base_color"}))
                self.assertFalse(result["complete"])
                self.assertEqual(_codes(result), ["FILE_PATH_UNREADABLE"])

    def test_one_warning_per_map_even_when_shared_by_slots(self):
        graph = _graph({"class": "Autodesk Bitmap", "name": "Shared", "slot": "Generic_Image"})
        graph["edges"].append({**graph["edges"][0], "slot": "Bump_Image", "slotKey": "map:9"})
        result = impl.roles_from_payload(graph)
        self.assertEqual(_codes(result), ["FILE_PATH_UNREADABLE"])
        self.assertEqual(len(result["roles"]), 2)

    def test_procedural_maps_are_not_flagged(self):
        for cls in ("Noise", "Checker", "Color Correction", "Falloff", "Autodesk Checker", "CoronaColor"):
            with self.subTest(cls=cls):
                result = impl.roles_from_payload(_graph({"class": cls, "name": "Proc", "slot": "Bump_Image"}))
                self.assertTrue(result["complete"])
                self.assertEqual(result["warnings"], [])

    def test_map_with_path_is_not_flagged(self):
        path = r"C:\maps\oak_diffuse.jpg"
        result = impl.roles_from_payload(_graph({
            "class": "Autodesk Bitmap", "name": "Generic_Image", "slot": "Generic_Image",
            "files": [_file(path, "auxFiles")]}))
        self.assertTrue(result["complete"])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["roles"][0]["file"], path)
        self.assertEqual(result["roles"][0]["file_parameter"], "auxFiles")

    def test_missing_file_is_not_an_unreadable_path(self):
        result = impl.roles_from_payload(_graph({
            "class": "Bitmap", "name": "Map #2", "slot": "base_color",
            "files": [_file(r"C:\gone\wood.png", exists=False)]}))
        self.assertEqual(result["warnings"], [])
        self.assertIs(result["roles"][0]["exists"], False)

    def test_osl_bitmap_loader_without_image_is_flagged(self):
        shader = r"C:\Program Files\Autodesk\3ds Max 2026\OSL\UberBitmap2.osl"
        result = impl.roles_from_payload(_graph({
            "class": "OSL Map", "name": "Uber", "slot": "base_color",
            "files": [_file(shader, "OSLPath")]}))
        self.assertFalse(result["complete"])
        self.assertEqual(_codes(result), ["FILE_PATH_UNREADABLE"])

    def test_osl_loader_with_image_and_procedural_osl_are_not_flagged(self):
        loader = r"C:\OSL\OSLBitmap2.osl"
        cases = {
            "with image": [_file(loader, "OSLPath"), _file(r"C:\maps\oak.png", "Filename")],
            "procedural": [_file(r"C:\OSL\Noise.osl", "OSLPath")],
        }
        for label, files in cases.items():
            with self.subTest(label):
                result = impl.roles_from_payload(_graph(
                    {"class": "OSL Map", "name": "O", "slot": "base_color", "files": files}))
                self.assertEqual(result["warnings"], [])
                self.assertTrue(result["complete"])

    def test_localized_label_matches_through_internal_class(self):
        result = impl.roles_from_payload(_graph(
            {"class": "Bitmap-Textur", "classInternal": "Bitmap", "name": "Map #3", "slot": "base_color"}))
        self.assertFalse(result["complete"])
        self.assertEqual(_codes(result), ["FILE_PATH_UNREADABLE"])

    def test_empty_file_parameters_are_unassigned_not_unreadable(self):
        result = impl.roles_from_payload(_graph(
            {"class": "Bitmap", "name": "Placeholder", "slot": "base_color", "fileUnset": True}))
        self.assertTrue(result["complete"])
        self.assertEqual(_codes(result), ["FILE_NOT_ASSIGNED"])
        self.assertIn("Placeholder", result["warnings"][0]["message"])

    def test_autodesk_bitmap_with_empty_parameters_stays_unreadable(self):
        result = impl.roles_from_payload(_graph(
            {"class": "Autodesk Bitmap", "name": "Generic_Image", "slot": "Generic_Image", "fileUnset": True}))
        self.assertFalse(result["complete"])
        self.assertEqual(_codes(result), ["FILE_PATH_UNREADABLE"])

    def test_osl_image_shaders_without_image_are_flagged(self):
        for stem in ("HDRIEnviron", "CameraProjector", "RandomBitmap2"):
            with self.subTest(stem):
                result = impl.roles_from_payload(_graph({
                    "class": "OSL Map", "name": stem, "slot": "base_color",
                    "files": [_file(rf"C:\OSL\{stem}.osl", "OSLPath")]}))
                self.assertFalse(result["complete"])
                self.assertEqual(_codes(result), ["FILE_PATH_UNREADABLE"])

    def _osl_loader_fed_by(self, feeder_files: list[dict]) -> dict:
        graph = _graph({"class": "OSL Map", "name": "Loader", "slot": "base_color",
                        "files": [_file(r"C:\OSL\OSLBitmap2.osl", "OSLPath")]})
        graph["nodes"].append({"id": "n2", "kind": "texture", "class": "OSL Map", "name": "Feeder",
                               "handle": "202", "files": feeder_files})
        graph["edges"].append({"parentId": "n1", "nodeId": "n2", "slot": "Filename_map", "slotKey": "map:0",
                               "kind": "map", "inputType": 2, "role": "map", "aliases": []})
        return impl.roles_from_payload(graph)

    def test_osl_loader_fed_by_connected_file_is_not_flagged(self):
        result = self._osl_loader_fed_by([_file(r"C:\OSL\SetFile.osl", "OSLPath"),
                                          _file(r"C:\maps\oak.png", "In")])
        self.assertEqual(result["warnings"], [])
        self.assertTrue(result["complete"])
        self.assertIn(r"C:\maps\oak.png", [r["file"] for r in result["roles"]])

    def test_osl_loader_fed_by_shader_only_input_is_flagged(self):
        result = self._osl_loader_fed_by([_file(r"C:\OSL\GetUVW.osl", "OSLPath")])
        self.assertFalse(result["complete"])
        self.assertEqual(_codes(result), ["FILE_PATH_UNREADABLE"])

    def test_existing_warnings_are_kept(self):
        graph = _graph({"class": "Autodesk Bitmap", "name": "Generic_Image", "slot": "Generic_Image"})
        graph["warnings"] = [{"code": "CIRCULAR_REF", "nodeId": "n0", "message": "loop"}]
        result = impl.roles_from_payload(graph)
        self.assertEqual(_codes(result), ["CIRCULAR_REF", "FILE_PATH_UNREADABLE"])


class ToolAggregationTests(unittest.TestCase):
    def test_tool_reports_incomplete_and_keeps_material_in_only_problems(self):
        from maxmcp.tools import material_roles as tool

        batch = {"graphVersion": 2, "checked": 2, "total": 2, "offset": 0, "next_offset": None, "failed": [],
                 "graphs": [_graph({"class": "Autodesk Bitmap", "name": "Generic_Image", "slot": "Generic_Image"}),
                            _graph({"class": "Noise", "name": "N", "slot": "Bump_Image"})]}
        with mock.patch.object(tool.impl, "fetch_graphs", return_value=batch):
            out = tool.material_roles(scan_scene=True, only_problems=True)
        self.assertFalse(out["complete"])
        self.assertEqual(out["returned"], 1)
        self.assertEqual(_codes(out["materials"][0]), ["FILE_PATH_UNREADABLE"])


if __name__ == "__main__":
    unittest.main()
