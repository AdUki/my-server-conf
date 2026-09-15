"""A slow reply must not turn one notification into two.

send_message is wrapped in a retry loop, and ntfy has no idempotency key, so
retrying a POST that already reached the server delivers the message twice --
while the log records only the attempt that eventually returned.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402

from ai_marketplace_monitor.ntfy import NtfyNotificationConfig  # noqa: E402


def config():
    c = NtfyNotificationConfig(name="me")
    c.ntfy_server = "https://ntfy.sh"
    c.ntfy_topic = "test-topic"
    c.message_format = "plain_text"
    return c


class TestNtfySend(unittest.TestCase):
    def test_read_timeout_is_not_retried(self):
        """The message was sent; only the answer was late."""
        with mock.patch.object(requests, "post",
                               side_effect=requests.exceptions.ReadTimeout()) as post:
            self.assertTrue(config().send_message("t", "m"))
            self.assertEqual(post.call_count, 1)

    def test_connect_failure_propagates_so_it_can_be_retried(self):
        """Never reached the server, so a retry cannot duplicate anything."""
        with mock.patch.object(requests, "post",
                               side_effect=requests.exceptions.ConnectTimeout()):
            with self.assertRaises(requests.exceptions.ConnectTimeout):
                config().send_message("t", "m")

    def test_rejected_message_is_reported_not_swallowed(self):
        response = mock.Mock()
        response.raise_for_status.side_effect = requests.exceptions.HTTPError("429")
        with mock.patch.object(requests, "post", return_value=response):
            with self.assertRaises(requests.exceptions.HTTPError):
                config().send_message("t", "m")

    def test_successful_send_reports_sent(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        with mock.patch.object(requests, "post", return_value=response):
            self.assertTrue(config().send_message("t", "m"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
