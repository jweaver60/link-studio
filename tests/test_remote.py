import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from link_studio.remote import RemoteServer


class RemoteServerTests(unittest.TestCase):
    def setUp(self):
        self.actions = []
        self.remote = RemoteServer(
            lambda: {"device": "Link 2", "zoom": 125},
            lambda action, value: self.actions.append((action, value)),
            bind="127.0.0.1",
        )
        self.remote.start()
        self.root = f"http://127.0.0.1:{self.remote.server.server_port}"

    def tearDown(self):
        self.remote.stop()

    def test_health_is_public_but_state_requires_pairing_token(self):
        with urlopen(f"{self.root}/health", timeout=2) as response:
            self.assertEqual(json.load(response), {"ok": True})
        with self.assertRaises(HTTPError) as context:
            urlopen(f"{self.root}/api/state", timeout=2)
        self.assertEqual(context.exception.code, 401)
        context.exception.close()

        with urlopen(f"{self.root}/api/state?token={self.remote.token}", timeout=2) as response:
            self.assertEqual(json.load(response)["state"]["zoom"], 125)

    def test_authenticated_action_is_dispatched(self):
        request = Request(
            f"{self.root}/api/action?token={self.remote.token}",
            data=json.dumps({"action": "zoom", "value": 175}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 202)
        self.assertEqual(self.actions, [("zoom", 175)])

    def test_restart_rotates_the_pairing_token(self):
        old_token = self.remote.token
        self.remote.stop()
        self.remote.start()
        self.root = f"http://127.0.0.1:{self.remote.server.server_port}"

        self.assertNotEqual(self.remote.token, old_token)
        with self.assertRaises(HTTPError) as context:
            urlopen(f"{self.root}/api/state?token={old_token}", timeout=2)
        self.assertEqual(context.exception.code, 401)
        context.exception.close()

    def test_non_ascii_token_header_is_rejected_without_crashing_handler(self):
        request = Request(
            f"{self.root}/api/state",
            headers={"X-Link-Studio-Token": "é"},
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(request, timeout=2)
        self.assertEqual(context.exception.code, 401)
        context.exception.close()


if __name__ == "__main__":
    unittest.main()
