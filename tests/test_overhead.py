"""Call-path overhead benchmark (preview of gate G2: enqueue under 20 microseconds at p99).

Measures the full adapter path an app pays for: LiveKit metric object -> event dict -> buffer.
Thresholds here are loose so CI machines don't flake; the printed numbers are what matter.
"""

from __future__ import annotations

import statistics
import time
import unittest

from helpers import ns, offline_client
from voicetoll import livekit


class OverheadTests(unittest.TestCase):
    def test_livekit_observe_cost(self):
        client = offline_client(buffer_size=200_000, batch_size=1_000_000)
        meter = livekit.attach(
            None,
            tenant="t",
            call_id="c",
            client=client,
            providers={"tts": ("elevenlabs", "eleven_flash_v2_5")},
        )
        metric = ns(
            type="tts_metrics",
            characters_count=188,
            audio_duration=11.8,
            ttfb=0.18,
            duration=0.9,
            cancelled=False,
            speech_id="sp1",
            request_id="r",
        )
        for _ in range(2_000):  # warm up
            meter.observe(metric)
        samples = []
        for _ in range(20_000):
            t0 = time.perf_counter_ns()
            meter.observe(metric)
            samples.append(time.perf_counter_ns() - t0)
        samples.sort()
        p50 = statistics.median(samples) / 1000
        p99 = samples[int(len(samples) * 0.99)] / 1000
        print(f"\nlivekit observe(): p50 {p50:.1f} us, p99 {p99:.1f} us")
        self.assertLess(p50, 50)
        self.assertLess(p99, 200)


if __name__ == "__main__":
    unittest.main()
