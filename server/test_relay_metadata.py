import unittest

from jev_server import native_replay_headers

CHAT = "11111111-2222-7333-8444-555555555555"
OTHER = "12345678-1234-1234-1234-123456789abc"


class RelayMetadata(unittest.TestCase):
    def test_valid_chat_uuid_restores_stable_session_and_thread(self):
        payload = {"prompt_cache_key": CHAT}
        self.assertEqual(native_replay_headers(payload, {}),
                         {"session_id": CHAT, "thread-id": CHAT})
        self.assertEqual(native_replay_headers(payload, {}), native_replay_headers(payload, {}))

    def test_explicit_native_identity_wins_and_credentials_never_forward(self):
        incoming = {"session-id": OTHER, "thread-id": OTHER,
                    "x-codex-installation-id": CHAT, "originator": "codex_desktop",
                    "authorization": "Bearer private-fixture", "chatgpt-account-id": "private",
                    "cookie": "private", "x-arbitrary": "private"}
        result = native_replay_headers({"prompt_cache_key": CHAT}, incoming)
        self.assertEqual(result, {"session-id": OTHER, "thread-id": OTHER,
                                 "x-codex-installation-id": CHAT, "originator": "codex_desktop"})

    def test_non_uuid_cache_keys_and_malformed_metadata_are_not_transmitted(self):
        for invalid in ("private task text", "", None, 42, "x" * 100000, CHAT + "\nInjected: yes"):
            with self.subTest(value=type(invalid)):
                self.assertEqual(native_replay_headers({"prompt_cache_key": invalid},
                    {"session_id": invalid, "thread-id": invalid,
                     "originator": "bad\r\nInjected: yes"}), {})


if __name__ == "__main__":
    unittest.main()
