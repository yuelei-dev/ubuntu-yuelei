# -*- coding: utf-8 -*-
import importlib.util
import os
import pathlib
import unittest
from unittest.mock import Mock, patch

try:
    import requests
except ImportError as error:  # CI installs Hermes dependencies in a later step.
    raise unittest.SkipTest("requests is required for Hermes egress tests") from error


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "server" / "hermes_ip12" / "openai_egress.py"
SPEC = importlib.util.spec_from_file_location("hermes_openai_egress", MODULE)
egress = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(egress)


class HermesOpenAIEgressTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "HERMES_EGRESS_PROXY": "",
                "HERMES_EGRESS_PROXY_FALLBACK": "",
                "HERMES_OPENAI_OFFICIAL_BASE": "",
                "HERMES_OPENAI_RELAY_BASE": "",
                "HERMES_OPENAI_CONNECT_TIMEOUT": "8",
                "EGRESS_PROXY": "",
                "EGRESS_PROXY_FALLBACK": "",
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    @staticmethod
    def response(status=200, text="ok"):
        response = Mock()
        response.status_code = status
        response.text = text
        return response

    def test_unconfigured_chain_preserves_one_direct_request(self):
        expected = self.response()
        with patch.object(egress, "_post_request", return_value=expected) as post:
            actual = egress.post_chat_completions(
                "https://api.openai.com/v1", "test-key", {"stream": False}
            )
        self.assertIs(actual, expected)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], "https://api.openai.com/v1/chat/completions")
        self.assertIsNone(post.call_args.kwargs["proxy"])

    def test_connect_timeout_fails_over_from_proxy_to_direct(self):
        os.environ["HERMES_EGRESS_PROXY"] = "http://127.0.0.1:10809"
        expected = self.response()
        with patch.object(egress, "_proxy_reachable", return_value=True), patch.object(
            egress,
            "_post_request",
            side_effect=[requests.exceptions.ConnectTimeout("connect"), expected],
        ) as post:
            actual = egress.post_chat_completions(
                "https://api.openai.com/v1", "test-key", {"stream": False}
            )
        self.assertIs(actual, expected)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["proxy"], "http://127.0.0.1:10809")
        self.assertIsNone(post.call_args_list[1].kwargs["proxy"])

    def test_unreachable_proxy_is_skipped_without_sending(self):
        os.environ["HERMES_EGRESS_PROXY"] = "http://127.0.0.1:10809"
        expected = self.response()
        with patch.object(egress, "_proxy_reachable", return_value=False), patch.object(
            egress, "_post_request", return_value=expected
        ) as post:
            actual = egress.post_chat_completions(
                "https://api.openai.com/v1", "test-key", {"stream": False}
            )
        self.assertIs(actual, expected)
        self.assertEqual(post.call_count, 1)
        self.assertIsNone(post.call_args.kwargs["proxy"])

    def test_read_timeout_is_ambiguous_and_is_not_resent(self):
        os.environ["HERMES_EGRESS_PROXY"] = "http://127.0.0.1:10809"
        os.environ["HERMES_OPENAI_RELAY_BASE"] = "https://relay.example/openai"
        with patch.object(egress, "_proxy_reachable", return_value=True), patch.object(
            egress,
            "_post_request",
            side_effect=requests.exceptions.ReadTimeout("read"),
        ) as post:
            with self.assertRaises(requests.exceptions.ReadTimeout):
                egress.post_chat_completions(
                    "https://api.openai.com/v1", "test-key", {"stream": False}
                )
        self.assertEqual(post.call_count, 1)

    def test_tls_failure_is_ambiguous_and_is_not_resent(self):
        os.environ["HERMES_OPENAI_RELAY_BASE"] = "https://relay.example/openai"
        with patch.object(
            egress,
            "_post_request",
            side_effect=requests.exceptions.SSLError("tls"),
        ) as post:
            with self.assertRaises(requests.exceptions.SSLError):
                egress.post_chat_completions(
                    "https://api.openai.com/v1", "test-key", {"stream": False}
                )
        self.assertEqual(post.call_count, 1)

    def test_connection_reset_is_ambiguous_and_is_not_resent(self):
        os.environ["HERMES_OPENAI_RELAY_BASE"] = "https://relay.example/openai"
        with patch.object(
            egress, "_post_request", side_effect=ConnectionResetError("reset")
        ) as post:
            with self.assertRaises(ConnectionResetError):
                egress.post_chat_completions(
                    "https://api.openai.com/v1", "test-key", {"stream": False}
                )
        self.assertEqual(post.call_count, 1)

    def test_http_error_proves_delivery_and_is_not_resent(self):
        os.environ["HERMES_OPENAI_RELAY_BASE"] = "https://relay.example/openai"
        with patch.object(
            egress, "_post_request", return_value=self.response(503, "upstream busy")
        ) as post:
            with self.assertRaisesRegex(RuntimeError, "API 503: upstream busy"):
                egress.post_chat_completions(
                    "https://api.openai.com/v1", "test-key", {"stream": False}
                )
        self.assertEqual(post.call_count, 1)

    def test_relay_precedes_configured_base_and_normalizes_v1(self):
        os.environ["HERMES_OPENAI_RELAY_BASE"] = "https://relay.example/openai"
        expected = self.response()
        network_error = ConnectionRefusedError("connection refused")
        with patch.object(
            egress, "_post_request", side_effect=[network_error, expected]
        ) as post:
            actual = egress.post_chat_completions(
                "https://api.openai.com/v1", "test-key", {"stream": False}
            )
        self.assertIs(actual, expected)
        self.assertEqual(
            [call.args[0] for call in post.call_args_list],
            [
                "https://relay.example/openai/v1/chat/completions",
                "https://api.openai.com/v1/chat/completions",
            ],
        )

    def test_streaming_response_and_flag_are_preserved(self):
        expected = self.response()
        with patch.object(egress, "_post_request", return_value=expected) as post:
            actual = egress.post_chat_completions(
                "https://api.openai.com", "test-key", {"stream": True}, stream=True
            )
        self.assertIs(actual, expected)
        self.assertTrue(post.call_args.kwargs["stream"])
        self.assertEqual(post.call_args.kwargs["timeout"], (8.0, 180.0))


if __name__ == "__main__":
    unittest.main()
