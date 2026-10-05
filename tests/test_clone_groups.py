"""Tests for clone_objects on group members (issue #16)."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# The embedded runtime ignores PYTHONPATH; prefer the checkout over an installed maxmcp.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxmcp.tools import clone as cl  # noqa: E402


def _member(index, group="GRP_Vitrine_Props", handle=101, cs=1, open_=False):
    return {"i": index, "cs": cs, "handle": str(handle), "group": group, "open": open_}


def _shadow(index, member):
    return {"i": index, "cs": 0, "shadow": member}


def _made(handles, members=("101",), missing=()):
    return json.dumps({"handles": list(handles), "members": list(members), "missing": list(missing)})


def _spatial(names):
    return json.dumps({"nodes": [{"name": n, "class": "Editable_Poly"} for n in names], "space": {}})


def _client(native, results):
    client = MagicMock()
    client.native_available = native
    client.send_command.side_effect = [{"result": r} for r in results]
    return client


def _scripts(client):
    return [c.args[0] for c in client.send_command.call_args_list]


class ImportTargetTests(unittest.TestCase):
    def test_imports_checkout(self):
        repo = Path(__file__).resolve().parent.parent
        self.assertEqual(Path(cl.__file__).resolve(), repo / "maxmcp" / "tools" / "clone.py")


class GroupMemberTests(unittest.TestCase):
    def test_member_uses_safe_path(self):
        results = [json.dumps([_member(1)]),
                   _made(["900"]),
                   _spatial(["Vase002"])]
        with patch.object(cl, "client", _client(True, results)) as client:
            out = json.loads(cl.clone_objects(["Vase01"], mode="instance", offset=[10, 0, 0]))
        scripts = _scripts(client)
        self.assertEqual(len(scripts), 3)
        self.assertIn("isGroupMember", scripts[0])
        clone_ms = scripts[1]
        self.assertNotIn("maxOps.cloneNodes", clone_ms)
        self.assertIn("c.parent = undefined", clone_ms)
        self.assertIn("setGroupMember c false", clone_ms)
        self.assertIn("instance s", clone_ms)
        self.assertIn("undo \"Clone group member\" on", clone_ms)
        self.assertIn("[10,0,0]", clone_ms)
        self.assertIn("#(101)", clone_ms)
        for call in client.send_command.call_args_list:
            self.assertNotEqual(call.kwargs.get("cmd_type"), "native:clone_objects")
        self.assertEqual(out["cloned"], ["Vase002"])
        self.assertTrue(out["detached_from_group"])
        self.assertEqual(out["group_members"],
                         [{"name": "Vase01", "group": "GRP_Vitrine_Props", "open_group": False}])
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("clone_whole_group", out["warnings"][0])
        self.assertIn("GRP_Vitrine_Props", out["warnings"][0])

    def test_member_array_count(self):
        results = [json.dumps([_member(1)]),
                   _made(["900", "901", "902"]),
                   _spatial(["Vase002", "Vase003", "Vase004"])]
        with patch.object(cl, "client", _client(False, results)) as client:
            out = json.loads(cl.clone_objects(["Vase01"], mode="copy", offset=[0, 5, 0], count=3))
        clone_ms = _scripts(client)[1]
        self.assertIn("for step = 1 to 3 do", clone_ms)
        self.assertIn("copy s", clone_ms)
        self.assertIn("true and missing.count", clone_ms)
        self.assertNotIn("maxOps.cloneNodes", clone_ms)
        self.assertEqual(out["count"], 3)
        self.assertEqual(len(out["cloned"]), 3)

    def test_mixed_request_keeps_plain_nodes_on_clone_nodes(self):
        results = [json.dumps([_member(2)]),
                   _made(["900", "901"], missing=["Ghost"]),
                   _spatial(["Chair002", "Vase002"])]
        with patch.object(cl, "client", _client(True, results)) as client:
            out = json.loads(cl.clone_objects(["Chair01", "Vase01", "Ghost"]))
        clone_ms = _scripts(client)[1]
        self.assertIn('#("Chair01","Ghost")', clone_ms)
        self.assertIn("maxOps.cloneNodes plainSrc", clone_ms)
        self.assertIn("expandHierarchy:true", clone_ms)
        self.assertEqual(out["notFound"], ["Ghost"])
        self.assertEqual([g["name"] for g in out["group_members"]], ["Vase01"])

    def test_error_raises_and_reports(self):
        results = [json.dumps([_member(1)]), "__ERROR__|Could not detach copy of Vase01 from its group"]
        with patch.object(cl, "client", _client(True, results)):
            with self.assertRaisesRegex(RuntimeError, "detach"):
                cl.clone_objects(["Vase01"])

    def test_script_cleans_up_on_failure(self):
        ms = cl.build_group_member_clone_maxscript([], [101], "copy", [0, 0, 0], 1)
        self.assertIn("for n in made where isValidNode n do delete n", ms)
        self.assertNotIn("maxOps.cloneNodes", ms)
        self.assertNotIn("__", ms.replace("__ERROR__", ""))  # every placeholder filled

    def test_ambiguous_member_name_refused(self):
        results = [json.dumps([_member(1, cs=2)])]
        with patch.object(cl, "client", _client(True, results)) as client:
            with self.assertRaisesRegex(ValueError, "unique"):
                cl.clone_objects(["Vase01"])
        self.assertEqual(client.send_command.call_count, 1)

    def test_bad_detection_output_raises(self):
        with patch.object(cl, "client", _client(True, ["oops"])):
            with self.assertRaises(RuntimeError):
                cl.clone_objects(["Vase01"])


class UnchangedPathTests(unittest.TestCase):
    NATIVE = {"cloned": ["Box002"], "notFound": [], "count": 1,
              "nodes": [{"name": "Box002", "class": "Box"}], "space": {}}

    def test_non_member_uses_native(self):
        with patch.object(cl, "client", _client(True, ["[]", json.dumps(self.NATIVE)])) as client:
            out = json.loads(cl.clone_objects(["Box001"], offset=[1, 2, 3]))
        calls = client.send_command.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].kwargs["cmd_type"], "native:clone_objects")
        self.assertEqual(json.loads(calls[1].args[0]), {"names": ["Box001"], "mode": "copy", "offset": [1, 2, 3]})
        self.assertEqual(out["cloned"], ["Box002"])
        self.assertNotIn("group_members", out)
        self.assertNotIn("warnings", out)

    def test_group_head_uses_native(self):
        # A top-level group head is not a group member: detection returns [].
        with patch.object(cl, "client", _client(True, ["[]", json.dumps(self.NATIVE)])) as client:
            cl.clone_objects(["GRP_Vitrine_Props"])
        self.assertEqual(client.send_command.call_args_list[1].kwargs["cmd_type"], "native:clone_objects")

    def test_detection_script_targets_members_not_heads(self):
        with patch.object(cl, "client", _client(True, ["[]", json.dumps(self.NATIVE)])) as client:
            cl.clone_objects(["GRP_Vitrine_Props"])
        detect = _scripts(client)[0]
        self.assertIn("fn mcpIsMember n = (isGroupMember n) or (mcpInClosedGroup n)", detect)
        self.assertIn("ignoreCase:false", detect)
        self.assertIn('#("GRP_Vitrine_Props")', detect)

    def test_non_member_array_unchanged(self):
        with patch.object(cl, "client", _client(True, ["[]", "900,901", _spatial(["Box002", "Box003"])])) as client:
            out = json.loads(cl.clone_objects(["Box001"], offset=[5, 0, 0], count=2))
        array_ms = _scripts(client)[1]
        self.assertIn("maxOps.cloneNodes src", array_ms)
        self.assertIn('undo "Clone array" on', array_ms)
        self.assertEqual(out["cloned"], ["Box002", "Box003"])

    def test_clone_whole_group_skips_detection(self):
        with patch.object(cl, "client", _client(True, [json.dumps(self.NATIVE)])) as client:
            out = json.loads(cl.clone_objects(["Vase01"], clone_whole_group=True))
        calls = client.send_command.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs["cmd_type"], "native:clone_objects")
        self.assertNotIn("detached_from_group", out)

    def test_clone_whole_group_array_uses_clone_nodes(self):
        with patch.object(cl, "client", _client(True, ["900,901", _spatial(["A", "B"])])) as client:
            cl.clone_objects(["Vase01"], count=2, clone_whole_group=True)
        self.assertIn("maxOps.cloneNodes src", _scripts(client)[0])


class ReviewFixTests(unittest.TestCase):
    def test_clone_script_guards_untracked_nodes(self):
        ms = cl.build_group_member_clone_maxscript(["Chair01"], [101], "instance", [0, 0, 0], 2)
        self.assertIn("local preNodes = objects as array", ms)
        self.assertIn("objects.count - preNodes.count - made.count", ms)
        self.assertIn("mcpDeleteNew preNodes", ms)
        self.assertNotIn("__", ms.replace("__ERROR__", ""))

    def test_clone_script_rejects_plain_members_and_dedupes(self):
        ms = cl.build_group_member_clone_maxscript(["Chair01"], [101, 101], "copy", [0, 0, 0], 1)
        self.assertIn("else if mcpIsMember matches[1] then append plainMembers nm", ms)
        self.assertIn("appendIfUnique memberSrc n", ms)
        self.assertIn("getNodeByName nm exact:true ignoreCase:false all:true", ms)
        self.assertNotIn("getNodeByName nm exact:true all:true", ms)

    def test_detection_maps_by_index_not_string(self):
        # A mangled name in the reply cannot move a member into the plain list.
        results = [json.dumps([dict(_member(1), name="garbled")]), _made(["900"]), _spatial(["Vase002"])]
        with patch.object(cl, "client", _client(True, results)) as client:
            out = json.loads(cl.clone_objects(["Vase01"]))
        clone_ms = _scripts(client)[1]
        self.assertNotIn("maxOps.cloneNodes", clone_ms)
        self.assertEqual(out["group_members"][0]["name"], "Vase01")

    def test_duplicate_member_handles_cloned_once(self):
        results = [json.dumps([_member(1), _member(2)]), _made(["900"]), _spatial(["Vase002"])]
        with patch.object(cl, "client", _client(True, results)) as client:
            out = json.loads(cl.clone_objects(["Vase01", "Vase01x"]))
        self.assertIn("#(101)", _scripts(client)[1])
        self.assertEqual(len(out["group_members"]), 1)

    def test_case_differing_plain_node_not_refused(self):
        # 'Chair01' exact plain node plus member 'CHAIR01': no refusal, cloned case-exactly.
        results = [json.dumps([_shadow(1, "CHAIR01")]), _made(["900"], members=[]), _spatial(["Chair002"])]
        with patch.object(cl, "client", _client(True, results)) as client:
            out = json.loads(cl.clone_objects(["Chair01"]))
        clone_ms = _scripts(client)[1]
        self.assertIn('#("Chair01")', clone_ms)
        self.assertIn("for h in #() do", clone_ms)  # no member handles
        self.assertEqual(out["cloned"], ["Chair002"])
        self.assertEqual(out["group_members"], [])
        self.assertFalse(out["detached_from_group"])
        self.assertIn("CHAIR01", out["warnings"][0])

    def test_wrong_case_member_name_not_cloned(self):
        # 'vase01' only matches member 'Vase01' ignoring case: notFound, nothing cloned.
        results = [json.dumps([_shadow(1, "Vase01")]), _made([], members=[], missing=["vase01"])]
        with patch.object(cl, "client", _client(True, results)) as client:
            out = json.loads(cl.clone_objects(["vase01"]))
        self.assertEqual(client.send_command.call_count, 2)
        self.assertEqual(out["error"], "No valid objects found to clone")
        self.assertEqual(out["notFound"], ["vase01"])
        for call in client.send_command.call_args_list:
            self.assertNotEqual(call.kwargs.get("cmd_type"), "native:clone_objects")

    def test_ambiguity_uses_case_exact_count(self):
        results = [json.dumps([_member(1, cs=1)]), _made(["900"]), _spatial(["Vase002"])]
        with patch.object(cl, "client", _client(True, results)):
            out = json.loads(cl.clone_objects(["Vase01"]))
        self.assertTrue(out["detached_from_group"])

    def test_open_group_member_warning(self):
        results = [json.dumps([_member(1, group="GRP_Open", open_=True)]), _made(["900"]), _spatial(["Vase002"])]
        with patch.object(cl, "client", _client(True, results)):
            out = json.loads(cl.clone_objects(["Vase01"]))
        self.assertEqual(out["group_members"], [{"name": "Vase01", "group": "GRP_Open", "open_group": True}])
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("Open-group", out["warnings"][0])
        self.assertNotIn("WHOLE closed group", out["warnings"][0])

    def test_member_under_requested_head_not_reported_detached(self):
        # GRP_A is plain (head); Box_A1 is dropped as its descendant inside the script.
        results = [json.dumps([_member(2, group="GRP_A")]),
                   _made(["900", "901", "902"], members=[]),
                   _spatial(["GRP_A001", "Box_A1001", "Box_A2001"])]
        with patch.object(cl, "client", _client(True, results)):
            out = json.loads(cl.clone_objects(["GRP_A", "Box_A1"]))
        self.assertEqual(out["group_members"], [])
        self.assertFalse(out["detached_from_group"])
        self.assertEqual(out["cloned_with_ancestor"], ["Box_A1"])
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("requested ancestor", out["warnings"][0])

    def test_clone_script_returns_alone_members(self):
        ms = cl.build_group_member_clone_maxscript([], [101], "copy", [0, 0, 0], 1)
        self.assertIn('\\"members\\":[', ms)


if __name__ == "__main__":
    unittest.main()
