import json
import xml.etree.ElementTree as ET
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings

from .services import azure_speech, speech


@override_settings(TTS_PROVIDER="azure", SESSION_COOKIE_SECURE=False, AZURE_TTS_VOICE_PT="", TTS_CACHE_TTL_SECONDS=0)
class LessonFlowTests(SimpleTestCase):
    sentence = "I think the weather is really nice today."

    def setUp(self):
        self.completion = self.enterContext(patch("teacher.services.llm._complete"))
        self.enterContext(patch("teacher.services.llm._client"))
        self.synth = self.enterContext(patch(
            "teacher.services.azure_speech.synthesize_with_visemes",
            side_effect=self.synthesize,
        ))
        self.respond(self.sentence)

    def respond(self, target):
        self.segments = [
            {"lang": "pt", "text": "Repete essa:"},
            {"lang": "en", "text": target},
        ]
        self.completion.return_value = json.dumps({
            "speech": self.segments, "target": target,
            "translation": "Frase de teste", "focus": "θ", "mood": "neutral",
        })

    def synthesize(self, segments, style=None):
        # Exercita a montagem real do SSML, sem chamar o serviço externo.
        xml = azure_speech.build_ssml(segments, "en-US-GuyNeural", style)
        self.assertIn('voice name="pt-BR-AntonioNeural"', xml)
        self.assertIn('voice name="en-US-GuyNeural"', xml)
        self.assertEqual(segments, self.segments)
        return {"audio_mp3": b"test-audio", "visemes": [[0, 1]]}

    def test_start_preserves_languages_and_saves_target(self):
        response = self.client.post("/api/start")
        self.assertEqual(response.status_code, 200)
        self.assertIn("llm;dur=", response["Server-Timing"])
        self.assertIn("tts;dur=", response["Server-Timing"])
        data = response.json()
        self.assertEqual(data["next_reference"], self.sentence)
        self.assertEqual(data["sentence"], self.sentence)
        self.assertEqual(data["speech"]["audio_b64"], "dGVzdC1hdWRpbw==")
        self.assertNotIn("tts_error", data)
        self.assertNotIn("_assistant_message", data)
        session = self.client.session
        self.assertEqual(session["coach"]["sentence"], self.sentence)
        self.assertEqual(json.loads(session["history"][-1]["content"])["speech"], self.segments)

    def test_tts_failure_keeps_text_and_target_available(self):
        self.synth.side_effect = RuntimeError("Voice unavailable")
        with self.assertLogs(level="ERROR"):
            data = self.client.post("/api/start").json()
        self.assertIsNone(data["speech"])
        self.assertEqual(data["tts_error"], "Voice unavailable")
        self.assertEqual(data["next_reference"], self.sentence)
        self.assertIn(self.sentence, data["reply"])

    def assess(self, summary):
        with patch("teacher.views.to_wav_16k", return_value=b"wav"), \
             patch("teacher.views.azure_speech.assess_pronunciation", return_value={}) as assess, \
             patch("teacher.views.summarize", return_value=summary):
            response = self.client.post("/api/assess", {
                "audio": SimpleUploadedFile("student.wav", b"audio"),
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn("assessment;dur=", response["Server-Timing"])
        assess.assert_called_once_with(b"wav", self.sentence)
        return response.json()

    def test_passing_sentence_saves_new_target(self):
        self.client.post("/api/start")
        target = "The red bird is over there."
        self.respond(target)
        data = self.assess({
            "recognized": True, "transcript": self.sentence,
            "scores": {"overall": 90}, "words": [], "issues": [],
        })
        self.assertEqual(data["event"], "sentence_pass")
        self.assertEqual(data["next_reference"], target)
        self.assertEqual(self.client.session["coach"]["sentence"], target)
        self.assertEqual(data["progress"]["done"], 1)
        self.assertNotIn("tts_error", data)
        self.assertNotIn("_assistant_message", data)
        self.assertEqual(json.loads(self.client.session["history"][-1]["content"])["target"], target)

    def test_word_practice_preserves_original_sentence(self):
        self.client.post("/api/start")
        self.respond("think")
        word = {"word": "think", "score": 30, "error": "Mispronunciation"}
        data = self.assess({
            "recognized": True, "transcript": self.sentence,
            "scores": {"overall": 50}, "words": [word], "issues": [word],
        })
        self.assertEqual(data["event"], "enter_word")
        self.assertEqual(data["next_reference"], "think")
        self.assertEqual(data["sentence"], self.sentence)
        self.assertEqual(self.client.session["coach"]["sentence"], self.sentence)
        self.assertNotIn("tts_error", data)

    def test_mislabeled_instruction_is_portuguese_before_synthesis(self):
        target = "She threw the ball through the narrow gate."
        self.segments = [
            {"lang": "pt", "text": "Agora tenta isso:"},
            {"lang": "en", "text": target},
        ]
        self.completion.return_value = json.dumps({
            "speech": [{"lang": "en", "text": "Agora tenta isso: " + target}],
            "target": target, "mood": "neutral",
        })
        data = self.client.post("/api/start").json()
        self.assertNotIn("tts_error", data)
        self.assertEqual(data["next_reference"], target)
        self.assertEqual(data["reply"], "Agora tenta isso: " + target)
        stored = json.loads(self.client.session["history"][-1]["content"])
        self.assertEqual(stored["speech"], self.segments)


@override_settings(AZURE_TTS_VOICE_PT="")
class SpeechLanguageTests(SimpleTestCase):
    def test_instruction_alone_and_common_variants(self):
        for instruction in ("Agora tenta isso:", "Repete comigo:", "Tenta de novo:"):
            with self.subTest(instruction=instruction):
                self.assertEqual(speech.normalize([{"lang": "en", "text": instruction}]),
                                 [{"lang": "pt", "text": instruction}])

    def test_english_examples_and_isolated_sounds_are_preserved(self):
        segments = [
            {"lang": "pt", "text": "Agora tenta isso:"},
            {"lang": "en", "text": "She threw the ball through the narrow gate."},
            {"lang": "en", "text": "Try this: repeat after me."},
            {"lang": "sound", "ipa": "θ", "text": "th"},
        ]
        self.assertEqual(speech.normalize(segments), segments)

    def test_corrected_segments_have_explicit_locales_in_hd_voice(self):
        segments = speech.normalize([{
            "lang": "en", "text": "Agora tenta isso: She threw the ball through the narrow gate.",
        }])
        xml = azure_speech.build_ssml(segments, "pt-BR-Antonio:DragonHDOmniLatestNeural")
        root = ET.fromstring(xml)
        langs = root.findall(".//{http://www.w3.org/2001/10/synthesis}lang")
        locale = "{http://www.w3.org/XML/1998/namespace}lang"
        self.assertEqual([(node.attrib[locale], node.text) for node in langs], [
            ("pt-BR", "Agora tenta isso:"),
            ("en-US", "She threw the ball through the narrow gate."),
        ])
        self.assertEqual(speech.normalize(segments), segments)
