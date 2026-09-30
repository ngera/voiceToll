from __future__ import annotations

import gzip
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from helpers import offline_client
from voicetoll.buffer import RingBuffer
from voicetoll.client import VoiceToll
from voicetoll.config import Config
from voicetoll.ids import pseudonymize


class BufferTests(unittest.TestCase):
    def test_drops_newest_when_full_and_counts(self):
        buf = RingBuffer(2)
        self.assertTrue(buf.put({"n": 1}))
        self.assertTrue(buf.put({"n": 2}))
        self.assertFalse(buf.put({"n": 3}))
        self.assertEqual(buf.dropped, 1)
        self.assertEqual([e["n"] for e in buf.take(10)], [1, 2])

    def test_requeue_front_keeps_order_and_counts_overflow(self):
        buf = RingBuffer(3)
        buf.put({"n": 3})
        lost = buf.requeue_front([{"n": 1}, {"n": 2}, {"n": 9}])
        self.assertEqual(lost, 1)
        self.assertEqual([e["n"] for e in buf.take(10)], [1, 2, 3])


class IdTests(unittest.TestCase):
    def test_pseudonymize_is_stable_and_hides_raw_value(self):
        a = pseudonymize("+1-555-0100", "k")
        self.assertEqual(a, pseudonymize("+1-555-0100", "k"))
        self.assertTrue(a.startswith("h:"))
        self.assertNotIn("555", a)
        self.assertEqual(pseudonymize(a, "k"), a)  # already pseudonymized passes through
        self.assertEqual(pseudonymize("acme", None), "acme")  # no key: sent as given


class BuildEventTests(unittest.TestCase):
    def test_filters_unknown_units_zeros_and_timings(self):
        client = offline_client()
        event = client.build_event(
            "tts",
            "elevenlabs",
            "eleven_flash_v2_5",
            {"characters": 188, "bogus": 5, "audio_output_seconds": 0, "input_tokens": None},
            session_id="s1",
            timing_ms={"ttfb": 183.4, "nonsense": 1},
            tags={"feature": "booking"},
        )
        self.assertEqual(event["units"], {"characters": 188})
        self.assertEqual(event["timing_ms"], {"ttfb": 183.4})
        self.assertEqual(event["tags"], {"env": "test", "region": "us-east", "feature": "booking"})
        self.assertEqual(event["src"], {"characters": "reported"})

    def test_disabled_client_is_a_no_op(self):
        client = offline_client(disabled=True)
        self.assertFalse(client.record("tts", "x", "y", {"characters": 1}, session_id="s"))
        self.assertEqual(len(client.buffer), 0)

    def test_enqueue_fails_open(self):
        client = offline_client()
        client.buffer = None  # type: ignore[assignment]  # simulate an internal bug
        self.assertFalse(client.enqueue({"x": 1}))
        self.assertEqual(client.errors, 1)


class _Capture(BaseHTTPRequestHandler):
    received: list = []
    fail_next = 0

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if _Capture.fail_next:
            _Capture.fail_next -= 1
            self.send_response(503)
            self.end_headers()
            return
        _Capture.received.append((dict(self.headers), json.loads(gzip.decompress(body))))
        self.send_response(202)
        self.end_headers()

    def log_message(self, *args):
        pass


class ExporterTests(unittest.TestCase):
    def setUp(self):
        _Capture.received = []
        _Capture.fail_next = 0
        self.server = HTTPServer(("127.0.0.1", 0), _Capture)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()

    def _client(self) -> VoiceToll:
        return VoiceToll(Config(endpoint=self.endpoint, project="p", ingest_key="k1", flush_interval=0.05))

    def test_background_export_sends_gzip_batch_with_key(self):
        client = self._client()
        for i in range(5):
            client.record("tts", "elevenlabs", "m", {"characters": 10 + i}, session_id="s1")
        deadline = time.time() + 3
        while time.time() < deadline and client.exporter.sent < 5:
            time.sleep(0.02)
        client.shutdown(1)
        self.assertEqual(client.exporter.sent, 5)
        headers, payload = _Capture.received[0]
        self.assertEqual(headers["X-Voicetoll-Key"], "k1")
        self.assertEqual(len(payload["events"]), 5)

    def test_flush_waits_for_a_batch_the_background_thread_is_sending(self):
        client = self._client()
        slow = threading.Event()
        real_send = client.exporter._send

        def delayed(batch):
            slow.set()
            time.sleep(0.3)  # the background thread's batch is still in flight when flush() starts
            return real_send(batch)

        client.exporter._send = delayed
        for i in range(3):
            client.record("tts", "elevenlabs", "m", {"characters": 10 + i}, session_id="s1")
        self.assertTrue(slow.wait(2))
        self.assertTrue(client.flush(timeout=3))
        self.assertEqual(client.exporter.sent, 3)  # counted before flush returned
        client.shutdown(1)

    def test_batches_carry_the_client_counters_and_nothing_else(self):
        client = self._client()
        client.buffer.dropped = 3  # as if the buffer had overflowed earlier
        client.record("tts", "elevenlabs", "m", {"characters": 10}, session_id="s1")
        deadline = time.time() + 3
        while time.time() < deadline and client.exporter.sent < 1:
            time.sleep(0.02)
        client.shutdown(1)
        _, payload = _Capture.received[0]
        stats = payload["client"]
        self.assertEqual(
            set(stats),
            {
                "client_id",
                "sdk_version",
                "dropped",
                "errors",
                "sent",
                "buffer_len",
                "buffer_max",
                "started_epoch",
            },
        )
        self.assertEqual(stats["dropped"], 3)
        self.assertEqual(stats["client_id"], client.client_id)

    def test_failed_batch_is_retried(self):
        _Capture.fail_next = 1
        client = self._client()
        client.record("tts", "elevenlabs", "m", {"characters": 10}, session_id="s1")
        deadline = time.time() + 5
        while time.time() < deadline and client.exporter.sent < 1:
            time.sleep(0.05)
        client.shutdown(1)
        self.assertEqual(client.exporter.sent, 1)
        self.assertGreaterEqual(client.exporter.failed_batches, 1)


if __name__ == "__main__":
    unittest.main()
