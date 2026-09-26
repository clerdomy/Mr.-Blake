import json
import random
import string
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.test import SimpleTestCase, override_settings
from openai import APIStatusError

from .services import clients, coach, llm, tts
from .services.session import COOKIE_VALUE_BUDGET, save_lesson
from .services.tts_cache import AudioCache


class ClientTests(SimpleTestCase):
    def test_same_configuration_reuses_client_but_key_change_does_not(self):
        clients._client.cache_clear()
        self.addCleanup(clients._client.cache_clear)
        with patch.object(clients, "OpenAI") as factory:
            factory.side_effect = [Mock(), Mock()]
            first = clients.get_client("test-one")
            self.assertIs(clients.get_client("test-one"), first)
            self.assertIsNot(clients.get_client("test-two"), first)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
        self.assertEqual(factory.call_args.kwargs["timeout"], 30)


class LLMRetryTests(SimpleTestCase):
    def setUp(self):
        llm._unsupported_options.cache_clear()
        self.addCleanup(llm._unsupported_options.cache_clear)
        self.client = Mock()
        self.create = self.client.chat.completions.create
        self.success = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"speech": []}'))])

    def error(self, status, **body):
        response = SimpleNamespace(status_code=status, request=Mock(), headers={})
        return APIStatusError(body.get("message", "failed"), response=response, body=body)

    def test_service_errors_are_not_retried_with_different_parameters(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                self.create.reset_mock()
                self.create.side_effect = self.error(status)
                with self.assertRaises(APIStatusError):
                    llm._complete(self.client, [])
                self.assertEqual(self.create.call_count, 1)

    def test_transport_error_is_not_retried(self):
        self.create.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            llm._complete(self.client, [])
        self.assertEqual(self.create.call_count, 1)

    def test_unsupported_parameter_is_omitted_on_following_turns(self):
        self.create.side_effect = [self.error(400, param="reasoning_effort",
            code="unsupported_parameter", message="reasoning_effort is not supported"), self.success, self.success]
        llm._complete(self.client, [])
        llm._complete(self.client, [])
        self.assertEqual(self.create.call_count, 3)
        self.assertNotIn("reasoning_effort", self.create.call_args.kwargs)
        self.assertIn("response_format", self.create.call_args.kwargs)

    def test_bad_prompt_does_not_trigger_parameter_fallback(self):
        self.create.side_effect = self.error(400, message="context length exceeded")
        with self.assertRaises(APIStatusError):
            llm._complete(self.client, [])
        self.assertEqual(self.create.call_count, 1)

    def test_json_generation_failure_does_not_disable_json_for_next_turn(self):
        self.create.side_effect = [self.error(400, code="json_validate_failed"), self.success, self.success]
        llm._complete(self.client, [])
        llm._complete(self.client, [])
        self.assertNotIn("response_format", self.create.call_args_list[1].kwargs)
        self.assertIn("response_format", self.create.call_args_list[2].kwargs)


@override_settings(TTS_PROVIDER="azure", TTS_CACHE_TTL_SECONDS=300, TTS_CACHE_MAX_BYTES=8388608)
class TTSPerformanceTests(SimpleTestCase):
    segments = [{"lang": "pt", "text": "Agora tenta isso:"}, {"lang": "en", "text": "think"}]

    def setUp(self):
        self.enterContext(patch.object(tts, "audio_cache", AudioCache()))

    def test_repeated_audio_uses_cache_and_voice_change_invalidates_it(self):
        with patch.object(tts, "_azure", return_value={"audio_mp3": b"audio", "visemes": [[0, 1]]}) as synth:
            tts.synthesize(self.segments)
            cached = tts.synthesize(self.segments)
            cached["visemes"][0][1] = 10
            self.assertEqual(tts.synthesize(self.segments)["visemes"], [[0, 1]])
            self.assertEqual(synth.call_count, 1)
            with override_settings(AZURE_TTS_VOICE_PT="another-voice"):
                tts.synthesize(self.segments)
            self.assertEqual(synth.call_count, 2)

    def test_failures_are_not_cached(self):
        with patch.object(tts, "_azure", side_effect=RuntimeError("unavailable")) as synth:
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    tts.synthesize(self.segments)
            self.assertEqual(synth.call_count, 2)

    def test_neutral_azure_error_does_not_synthesize_twice(self):
        with patch("teacher.services.azure_speech.synthesize_with_visemes", side_effect=RuntimeError("unavailable")) as synth:
            with self.assertRaises(RuntimeError):
                tts._azure(self.segments, "neutral")
            self.assertEqual(synth.call_count, 1)

    @override_settings(AZURE_TTS_VOICE="pt-BR-MacerioMultilingualNeural", AZURE_TTS_VOICE_PT="pt-BR-MacerioMultilingualNeural")
    def test_macerio_without_style_does_not_retry_identical_ssml(self):
        with patch("teacher.services.azure_speech.synthesize_with_visemes", side_effect=RuntimeError("unavailable")) as synth:
            with self.assertRaises(RuntimeError):
                tts._azure(self.segments, "happy")
            self.assertEqual(synth.call_count, 1)

    def test_merge_preserves_language_boundaries_and_isolated_sounds(self):
        segments = [
            {"lang": "pt", "text": "Agora"}, {"lang": "pt", "text": "tenta isso:"},
            {"lang": "en", "text": "She threw the ball."},
            {"lang": "sound", "text": "th", "ipa": "θ"},
            {"lang": "sound", "text": "th", "ipa": "θ"},
        ]
        result = tts._merge_segments(segments)
        self.assertEqual(result, [{"lang": "pt", "text": "Agora tenta isso:"}, *segments[2:]])
        self.assertEqual(segments[0]["text"], "Agora")

    def test_cache_expires_and_evicts_to_byte_limit(self):
        cache = AudioCache()
        with patch("teacher.services.tts_cache.monotonic", return_value=10):
            cache.put(b"one", {"audio_mp3": b"a" * 400, "visemes": []}, ttl=5, max_bytes=1000)
            cache.put(b"two", {"audio_mp3": b"b" * 400, "visemes": []}, ttl=5, max_bytes=1000)
            self.assertIsNone(cache.get(b"one"))
            self.assertIsNotNone(cache.get(b"two"))
            self.assertLessEqual(cache.size, 1000)
        with patch("teacher.services.tts_cache.monotonic", return_value=16):
            self.assertIsNone(cache.get(b"two"))
            self.assertEqual(cache.size, 0)


class LessonSessionTests(SimpleTestCase):
    def test_history_stays_valid_and_cookie_fits_after_long_lesson(self):
        rng = random.Random(5)
        text = lambda: "".join(rng.choices(string.ascii_letters + " ", k=1100))
        state = coach.accept_target(coach.new_state(), {"target": "I think the weather is nice."})
        history = []
        for turn in range(6):
            history.extend([
                {"role": "user", "content": json.dumps({"system": {"event": "word_retry", "used_sentences": [text()]}})},
                {"role": "assistant", "content": json.dumps({"speech": [{"lang": "pt", "text": text()}], "turn": turn})},
            ])
        session = SessionStore()
        save_lesson(session, state, history)
        session.save()
        self.assertLessEqual(len(session.session_key), COOKIE_VALUE_BUDGET)
        self.assertLess(len(session["history"]), len(history))
        self.assertEqual(json.loads(session["history"][-1]["content"]),
                         json.loads(history[-1]["content"]))
        self.assertEqual(session["coach"]["sentence"], state["sentence"])
        for message in session["history"]:
            content = json.loads(message["content"])
            if message["role"] == "user":
                self.assertNotIn("used_sentences", content["system"])
        self.assertEqual([m["role"] for m in session["history"]], ["user", "assistant"] * (len(session["history"]) // 2))

    def test_legacy_truncated_history_does_not_leave_orphan_messages(self):
        session = SessionStore()
        save_lesson(session, coach.new_state(), [
            {"role": "user", "content": '{"system":'},
            {"role": "assistant", "content": '{"speech":[]}'},
            {"role": "user", "content": '{"system":{"event":"word_retry"}}'},
            {"role": "assistant", "content": '{"speech":[]}'},
        ])
        self.assertEqual([m["role"] for m in session["history"]], ["user", "assistant"])
