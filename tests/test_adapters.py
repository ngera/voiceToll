"""Adapter mapping tests with duck-typed stand-ins for LiveKit and Pipecat objects.

These pin our reading of the frameworks' field names. M2 adds contract tests against real, pinned
LiveKit and Pipecat versions.
"""

from __future__ import annotations

import asyncio
import unittest

from helpers import drain, fake, ns, offline_client
from voicetoll import livekit, pipecat


class LiveKitTests(unittest.TestCase):
    def setUp(self):
        self.client = offline_client()
        session = ns(
            stt=fake("STT", model="nova-3", provider="Deepgram"),
            llm=fake("LLM", model="gpt-4o-mini", provider="openai"),
            tts=fake("TTS", model="eleven_flash_v2_5", provider="elevenlabs"),
        )
        self.meter = livekit.attach(
            session, tenant="clinic_17", call_id="room-1", feature="reception", client=self.client
        )

    def test_detects_providers_and_pseudonymizes_tenant(self):
        call = self.meter.call
        self.assertEqual(call.components["stt"], ("deepgram", "nova-3"))
        self.assertTrue(call.tenant.startswith("h:"))

    def test_turn_of_metrics_maps_to_events(self):
        m = self.meter
        m.observe(
            ns(type="eou_metrics", end_of_utterance_delay=0.42, transcription_delay=0.18, speech_id="sp1")
        )
        m.observe(ns(type="stt_metrics", audio_duration=3.1, duration=0.0, request_id="r0"))
        m.observe(
            ns(
                type="llm_metrics",
                prompt_tokens=890,
                completion_tokens=52,
                prompt_cached_tokens=0,
                ttft=0.31,
                duration=0.9,
                speech_id="sp1",
                request_id="r1",
            )
        )
        m.observe(
            ns(
                type="tts_metrics",
                characters_count=188,
                audio_duration=11.84,
                ttfb=0.183,
                duration=0.942,
                cancelled=False,
                speech_id="sp1",
                request_id="r2",
            )
        )
        m.observe(ns(type="vad_metrics", idle_time=1.0))  # ignored
        events = drain(self.client)
        self.assertEqual([e["component"] for e in events], ["turn", "stt", "llm", "tts"])
        turn_ev, stt, llm, tts = events
        self.assertEqual(turn_ev["timing_ms"], {"eou_delay": 420.0, "transcription_delay": 180.0})
        self.assertEqual(stt["units"], {"audio_input_seconds": 3.1})
        self.assertEqual(stt["src"], {"audio_input_seconds": "estimated"})
        self.assertEqual(llm["units"], {"input_tokens": 890, "output_tokens": 52})
        self.assertEqual(llm["timing_ms"]["ttft"], 310.0)
        self.assertEqual(tts["units"], {"characters": 188, "audio_output_seconds": 11.84})
        self.assertEqual((tts["provider"], tts["model"]), ("elevenlabs", "eleven_flash_v2_5"))
        self.assertEqual(tts["tags"]["feature"], "reception")
        self.assertEqual(llm["turn"], tts["turn"])
        self.assertEqual(turn_ev["turn"], 1)

    def test_realtime_model_is_s2s(self):
        client = offline_client()
        session = ns(llm=fake("RealtimeModel", model="gpt-realtime", provider="openai"))
        meter = livekit.attach(session, tenant="t", call_id="c", client=client)
        meter.observe(
            ns(
                type="realtime_model_metrics",
                input_tokens=1200,
                output_tokens=300,
                input_token_details=ns(audio_tokens=1000, cached_tokens=200),
                output_token_details=ns(audio_tokens=250),
                ttft=0.4,
                duration=1.1,
                cancelled=False,
            )
        )
        (event,) = drain(client)
        self.assertEqual(event["component"], "s2s")
        self.assertEqual(event["provider"], "openai")
        self.assertEqual(event["units"]["input_audio_tokens"], 1000)

    def test_metrics_host_names_are_normalized(self):
        # LiveKit 1.8 reports the OpenAI plugin's provider as the API host
        meta = ns(model_provider="api.openai.com", model_name="gpt-4o-mini")
        self.meter.observe(ns(type="llm_metrics", prompt_tokens=10, completion_tokens=2, metadata=meta))
        self.assertEqual(drain(self.client)[-1]["provider"], "openai")

    def test_explicit_providers_win_over_metrics_metadata(self):
        client = offline_client()
        meter = livekit.attach(
            None, tenant="t", call_id="c", client=client, providers={"llm": ("openai", "gpt-4o-mini")}
        )
        meta = ns(model_provider="something-else", model_name="other-model")
        meter.observe(ns(type="llm_metrics", prompt_tokens=10, completion_tokens=2, metadata=meta))
        event = drain(client)[-1]
        self.assertEqual((event["provider"], event["model"]), ("openai", "gpt-4o-mini"))

    def test_observe_never_raises(self):
        self.assertFalse(self.meter.observe(object()))
        self.assertFalse(self.meter.observe(None))


class FrameworkRegistryTests(unittest.TestCase):
    def test_builtins_are_registered_and_load_lazily(self):
        from voicetoll import frameworks

        found = frameworks.available()
        self.assertIn("livekit", found)
        self.assertIn("pipecat", found)
        self.assertIs(frameworks.load("livekit"), livekit.attach)
        self.assertIs(frameworks.load("pipecat"), pipecat.Observer)

    def test_old_import_paths_are_the_same_modules(self):
        from voicetoll.frameworks import livekit as lk_impl
        from voicetoll.frameworks import pipecat as pc_impl

        self.assertIs(livekit, lk_impl)
        self.assertIs(pipecat, pc_impl)

    def test_runtime_registration_and_unknown_name(self):
        from voicetoll import frameworks

        frameworks.register("example", "voicetoll.providers:normalize_provider", "test only")
        self.assertEqual(frameworks.load("example")("api.openai.com"), "openai")
        with self.assertRaises(KeyError):
            frameworks.load("no-such-framework")

    def test_events_carry_the_framework_name_as_source(self):
        client = offline_client()
        meter = livekit.attach(
            None, tenant="t", call_id="c", client=client, providers={"llm": ("openai", "m")}
        )
        meter.observe(ns(type="llm_metrics", prompt_tokens=1, completion_tokens=1))
        self.assertEqual(drain(client)[-1]["source"], "livekit")


class ProviderNameTests(unittest.TestCase):
    def test_shared_normalization(self):
        from voicetoll.providers import normalize_provider

        self.assertEqual(normalize_provider("api.openai.com"), "openai")
        self.assertEqual(normalize_provider("api.rime.ai"), "rime")
        self.assertEqual(normalize_provider("Azure-OpenAI"), "azure")
        self.assertEqual(normalize_provider("Deepgram"), "deepgram")
        self.assertIsNone(normalize_provider(""))
        self.assertIsNone(normalize_provider(None))


class PipecatTests(unittest.TestCase):
    def setUp(self):
        self.client = offline_client()
        self.obs = pipecat.Observer(
            tenant="tutortalk",
            user="learner-9",
            session_id="room-7",
            tags={"feature": "roleplay"},
            client=self.client,
        )
        self.obs._stt_usage_available = False  # Pipecat 0.0.x behaviour: STT seconds from PCM, per turn

    def push(self, frame):
        asyncio.run(self.obs.on_push_frame(ns(frame=frame)))

    def test_processor_name_parsing(self):
        self.assertEqual(pipecat.component_and_provider("DeepgramSTTService#0"), ("stt", "deepgram"))
        self.assertEqual(pipecat.component_and_provider("OpenAILLMService#1"), ("llm", "openai"))
        self.assertEqual(pipecat.component_and_provider("ElevenLabsTTSService"), ("tts", "elevenlabs"))
        self.assertEqual(pipecat.component_and_provider("SomethingElse"), (None, None))

    def test_processor_names_follow_pipecat_naming_convention(self):
        cases = {
            "ElevenLabsHttpTTSService#0": ("tts", "elevenlabs"),
            "CartesiaHttpTTSService#0": ("tts", "cartesia"),
            "DeepgramFluxSTTService#0": ("stt", "deepgram"),
            "AzureOpenAILLMService#0": ("llm", "azure"),
            "AWSTranscribeSTTService#0": ("stt", "aws"),
            "PollyTTSService#0": ("tts", "aws"),
            "GoogleSTTService#0": ("stt", "google"),
            "AssemblyAISTTService#0": ("stt", "assemblyai"),
        }
        for name, expected in cases.items():
            self.assertEqual(pipecat.component_and_provider(name), expected, name)

    def test_turn_produces_stt_llm_tts_events(self):
        audio = fake("InputAudioRawFrame", id=1, audio=b"\x00" * 64000, sample_rate=16000, num_channels=1)
        self.push(audio)
        self.push(audio)  # same frame on the next hop: counted once
        self.push(
            fake(
                "MetricsFrame",
                id=2,
                data=[fake("TTFBMetricsData", processor="DeepgramSTTService#0", model="nova-3", value=0.2)],
            )
        )
        self.push(fake("UserStoppedSpeakingFrame", id=3))
        self.push(
            fake(
                "MetricsFrame",
                id=4,
                data=[
                    fake("TTFBMetricsData", processor="OpenAILLMService#0", model="gpt-4o-mini", value=0.3),
                    fake(
                        "LLMUsageMetricsData",
                        processor="OpenAILLMService#0",
                        model="gpt-4o-mini",
                        value=ns(prompt_tokens=500, completion_tokens=40, cache_read_input_tokens=None),
                    ),
                ],
            )
        )
        self.push(
            fake(
                "MetricsFrame",
                id=5,
                data=[
                    fake("TTFBMetricsData", processor="CartesiaTTSService#0", model="sonic-2", value=0.15),
                    fake("TTSUsageMetricsData", processor="CartesiaTTSService#0", model="sonic-2", value=120),
                ],
            )
        )
        events = drain(self.client)
        self.assertEqual([e["component"] for e in events], ["stt", "llm", "tts"])
        stt, llm, tts = events
        self.assertEqual(stt["units"], {"audio_input_seconds": 2.0})  # 64,000 bytes of 16 kHz mono PCM16
        self.assertEqual((stt["provider"], stt["model"]), ("deepgram", "nova-3"))
        self.assertEqual(stt["timing_ms"]["ttfb"], 200.0)
        self.assertEqual(llm["units"], {"input_tokens": 500, "output_tokens": 40})
        self.assertEqual(llm["timing_ms"]["ttft"], 300.0)
        self.assertEqual(tts["units"], {"characters": 120})
        self.assertEqual(tts["provider"], "cartesia")
        self.assertTrue(tts["user"].startswith("h:"))
        self.assertEqual(tts["tags"]["feature"], "roleplay")


class PipecatOneXTests(unittest.TestCase):
    """Pipecat 1.x: STT usage metrics, net vs gross prompt tokens."""

    def make(self, stt_usage_available: bool):
        client = offline_client()
        obs = pipecat.Observer(tenant="t", session_id="p1", client=client)
        obs._stt_usage_available = stt_usage_available
        return client, obs

    def push(self, obs, frame):
        asyncio.run(obs.on_push_frame(ns(frame=frame)))

    def test_stt_usage_metrics_are_used_and_pcm_is_not_double_counted(self):
        client, obs = self.make(stt_usage_available=True)
        audio = fake("InputAudioRawFrame", id=1, audio=b"\x00" * 64000, sample_rate=16000, num_channels=1)
        self.push(obs, audio)
        usage = fake(
            "STTUsageMetricsData",
            processor="DeepgramSTTService#0",
            model="nova-3",
            value=ns(audio_seconds=2.4),
        )
        self.push(obs, fake("MetricsFrame", id=2, data=[usage]))
        self.push(obs, fake("UserStoppedSpeakingFrame", id=3))
        self.push(obs, fake("EndFrame", id=4))
        stt = [e for e in drain(client) if e["component"] == "stt"]
        self.assertEqual(len(stt), 1)
        self.assertEqual(stt[0]["units"], {"audio_input_seconds": 2.4})
        self.assertEqual(stt[0]["how"], {"audio_input_seconds": "pipecat_stt_usage"})

    def test_pcm_fallback_at_end_of_call_when_the_service_reports_no_usage(self):
        client, obs = self.make(stt_usage_available=True)
        audio = fake("InputAudioRawFrame", id=1, audio=b"\x00" * 64000, sample_rate=16000, num_channels=1)
        self.push(obs, audio)
        self.push(
            obs,
            fake(
                "MetricsFrame",
                id=2,
                data=[fake("TTFBMetricsData", processor="WhisperSTTService#0", model="small", value=0.2)],
            ),
        )
        self.push(obs, fake("UserStoppedSpeakingFrame", id=3))
        self.assertEqual([e for e in drain(client) if e["component"] == "stt"], [])  # held back
        self.push(obs, fake("EndFrame", id=4))
        (stt,) = [e for e in drain(client) if e["component"] == "stt"]
        self.assertEqual(stt["units"], {"audio_input_seconds": 2.0})

    def test_net_prompt_tokens_become_gross_input(self):
        client, obs = self.make(stt_usage_available=True)
        # Anthropic-style: prompt_tokens is net of the cache; total_tokens is gross
        usage = ns(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=950,
            cache_read_input_tokens=700,
            cache_creation_input_tokens=100,
        )
        self.push(
            obs,
            fake(
                "MetricsFrame",
                id=1,
                data=[
                    fake(
                        "LLMUsageMetricsData",
                        processor="AnthropicLLMService#0",
                        model="claude-sonnet-4-5",
                        value=usage,
                    )
                ],
            ),
        )
        (llm,) = drain(client)
        self.assertEqual(
            llm["units"],
            {"input_tokens": 900, "output_tokens": 50, "cache_read_tokens": 700, "cache_write_tokens": 100},
        )


if __name__ == "__main__":
    unittest.main()
