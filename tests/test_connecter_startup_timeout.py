import ast
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.connecter_startup_timeout import patched

SOURCE = '''def sendMessage_TCP(message):
    global jsonTcpPort
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect(('127.0.0.1', jsonTcpPort))
        s.sendall(json.dumps(message, default=lambda x: x.__dict__).encode('utf-8'))
'''


class ConnecterTimeoutTests(unittest.TestCase):
    def function(self, fail=False):
        sock = MagicMock()
        sock.gettimeout.return_value = None
        if fail:
            sock.connect.side_effect = socket.timeout("not connected")
        factory = MagicMock()
        factory.socket.return_value.__enter__.return_value = sock
        scope = {"socket": factory, "json": json, "jsonTcpPort": 54697}
        exec(compile(ast.parse(patched(SOURCE)), "test-connecter", "exec"), scope)
        return scope["sendMessage_TCP"], sock

    def test_connected_sends_original_json_and_restores_timeout(self):
        fn, sock = self.function()
        fn({"hello": "world"})
        self.assertEqual([c.args[0] for c in sock.settimeout.call_args_list], [0.25, None])
        sock.connect.assert_called_once_with(('127.0.0.1', 54697))
        sock.sendall.assert_called_once_with(b'{"hello": "world"}')

    def test_connect_timeout_does_not_send_and_restores_timeout(self):
        fn, sock = self.function(fail=True)
        with self.assertRaises(socket.timeout):
            fn({"hello": "world"})
        sock.sendall.assert_not_called()
        self.assertEqual([c.args[0] for c in sock.settimeout.call_args_list], [0.25, None])

    def test_idempotent_patch_refuses(self):
        with self.assertRaises(ValueError):
            patched(patched(SOURCE))

    def test_vendor_change_refused(self):
        with self.assertRaises(ValueError):
            patched(SOURCE.replace("127.0.0.1", "different-host"))


if __name__ == "__main__":
    unittest.main()
