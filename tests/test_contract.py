"""Contract tests against pinned LiveKit Agents / Pipecat field names.

Skipped when the optional extras are not installed (`uv sync --extra contract` or
`pip install 'voicetoll[contract]'`). Stand-in mapping coverage lives in test_adapters.py.
"""

from __future__ import annotations

import unittest

from helpers import drain, offline_client


def _has_livekit() -> bool:
    try:
        import livekit.agents.metrics  # noqa: F401

        return True
    except ImportError:
        return False


def _has_pipecat() -> bool:
    try:
        import pipecat.metrics.metrics  # noqa: F401

        return True
    except ImportError:
        return False


@unittest.skipUnless(_has_livekit(), "livekit-agents not installed")
class LiveKitContractTests(unittest.TestCase):
    """Pin attribute names our adapter reads from LiveKit Agents 1.x metric objects."""

    def test_metric_classes_expose_expected_fields(self):
        from livekit.agents import metrics as m

        # Class names and type strings the adapter keys on
        for cls_name, type_str, required in (
            ("STTMetrics", "stt_metrics", ("audio_duration", "duration", "request_id", "speech_id")),
            (
                "LLMMetrics",
                "llm_metrics",
                ("prompt_tokens", "completion_tokens", "ttft", "duration", "speech_id"),
            ),
            (
                "TTSMetrics",
                "tts_metrics",
                ("characters_count", "audio_duration", "ttfb", "duration", "cancelled"),
            ),
            ("EOUMetrics", "eou_metrics", ("end_of_utterance_delay", "transcription_delay", "speech_id")),
        ):
            cls = getattr(m, cls_name)
            fields = getattr(cls, "model_fields", None) or getattr(cls, "__annotations__", {})
            for name in required:
                self.assertIn(name, fields, f"{cls_name} missing {name}")
            # Instantiable with zeros for a smoke pass through our mapper
            kwargs = {f: (0 if f != "cancelled" else False) for f in required}
            if "type" in (getattr(cls, "model_fields", None) or {}):
                kwargs["type"] = type_str
            try:
                obj = cls(**kwargs)
            except Exception:
                # Some versions require more fields; build a namespace with the same attrs
                from helpers import ns

                obj = ns(type=type_str, **kwargs)
            client = offline_client()
            from voicetoll import livekit

            meter = livekit.attach(
                None,
                tenant="t",
                call_id="c",
                client=client,
                providers={
                    "stt": ("deepgram", "nova-3"),
                    "llm": ("openai", "gpt-4o-mini"),
                    "tts": ("elevenlabs", "eleven_flash_v2_5"),
                },
            )
            meter.observe(obj)
            # At least one event or a no-op for incomplete objects is fine; we care that observe does not raise
            drain(client)


@unittest.skipUnless(_has_pipecat(), "pipecat-ai not installed")
class PipecatContractTests(unittest.TestCase):
    """Pin class names our observer matches for Pipecat metrics frames."""

    def test_metrics_data_classes_exist(self):
        from pipecat.metrics import metrics as pm

        for name in (
            "TTFBMetricsData",
            "ProcessingMetricsData",
            "LLMUsageMetricsData",
            "TTSUsageMetricsData",
        ):
            self.assertTrue(hasattr(pm, name), f"pipecat.metrics.metrics missing {name}")
            cls = getattr(pm, name)
            fields = getattr(cls, "model_fields", None) or getattr(cls, "__annotations__", {})
            self.assertIn("processor", fields)
            if name == "LLMUsageMetricsData":
                # value holds token counts
                self.assertTrue("value" in fields or "prompt_tokens" in fields)

    def test_one_x_usage_fields_the_observer_reads(self):
        from pipecat.metrics import metrics as pm

        if not hasattr(pm, "STTUsageMetricsData"):
            self.skipTest("Pipecat before 1.x: no STT usage metrics (PCM counting is used)")
        self.assertIn("audio_seconds", pm.STTUsage.model_fields)
        for field in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            self.assertIn(field, pm.LLMTokenUsage.model_fields)

    def test_real_frames_through_the_observer(self):
        import asyncio

        from pipecat.frames.frames import EndFrame, InputAudioRawFrame, MetricsFrame, UserStoppedSpeakingFrame
        from pipecat.metrics import metrics as pm
        from pipecat.observers.base_observer import FramePushed
        from voicetoll import pipecat as vt_pipecat

        client = offline_client()
        obs = vt_pipecat.Observer(tenant="t", session_id="c", client=client)

        def push(frame):
            asyncio.run(
                obs.on_push_frame(
                    FramePushed(source=None, destination=None, frame=frame, direction=None, timestamp=0)
                )
            )

        push(InputAudioRawFrame(audio=b"\x00" * 32000, sample_rate=16000, num_channels=1))
        if hasattr(pm, "STTUsageMetricsData"):
            push(
                MetricsFrame(
                    data=[
                        pm.STTUsageMetricsData(
                            processor="DeepgramSTTService#0",
                            model="nova-3",
                            value=pm.STTUsage(audio_seconds=1.25),
                        )
                    ]
                )
            )
        push(UserStoppedSpeakingFrame())
        usage = pm.LLMTokenUsage(prompt_tokens=500, completion_tokens=40, total_tokens=540)
        push(
            MetricsFrame(
                data=[
                    pm.LLMUsageMetricsData(processor="OpenAILLMService#0", model="gpt-4o-mini", value=usage)
                ]
            )
        )
        push(
            MetricsFrame(
                data=[
                    pm.TTSUsageMetricsData(
                        processor="ElevenLabsTTSService#0", model="eleven_flash_v2_5", value=120
                    )
                ]
            )
        )
        push(EndFrame())
        events = {e["component"]: e for e in drain(client)}
        self.assertEqual(set(events), {"stt", "llm", "tts"})
        self.assertEqual(events["llm"]["units"], {"input_tokens": 500, "output_tokens": 40})
        self.assertEqual(
            (events["tts"]["provider"], events["tts"]["units"]), ("elevenlabs", {"characters": 120})
        )
        self.assertEqual(events["stt"]["provider"], "deepgram")


if __name__ == "__main__":
    unittest.main()
