"""
Azure Speech: avaliação de pronúncia (Pronunciation Assessment) e TTS com visemas.
A fala chega em trechos com idioma definido pelo LLM (ver speech.py).
Docs: https://learn.microsoft.com/azure/ai-services/speech-service/how-to-pronunciation-assessment
"""
import io
import json
import logging
import wave

import azure.cognitiveservices.speech as speechsdk
from django.conf import settings

from . import speech

log = logging.getLogger(__name__)


def _speech_config() -> speechsdk.SpeechConfig:
    if not settings.AZURE_SPEECH_KEY:
        raise RuntimeError("AZURE_SPEECH_KEY não configurada.")
    return speechsdk.SpeechConfig(
        subscription=settings.AZURE_SPEECH_KEY, region=settings.AZURE_SPEECH_REGION
    )


# ---------------------------------------------------------------------------
# Avaliação de pronúncia
# ---------------------------------------------------------------------------
def assess_pronunciation(wav_bytes: bytes, reference_text: str, language: str = "en-US") -> dict:
    """
    Avalia `wav_bytes` (WAV 16k mono) contra `reference_text`.
    Devolve o JSON bruto do Azure (NBest[0] com Words/Phonemes) já parseado,
    mais a transcrição reconhecida.
    """
    cfg = _speech_config()
    cfg.speech_recognition_language = language

    # Sem arquivo temporário (no Windows o SDK segura o arquivo aberto e o
    # unlink falha com WinError 32). Lê os frames PCM do WAV e empurra num stream.
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
        fmt = speechsdk.audio.AudioStreamFormat(
            samples_per_second=wf.getframerate(),
            bits_per_sample=wf.getsampwidth() * 8,
            channels=wf.getnchannels(),
        )
    stream = speechsdk.audio.PushAudioInputStream(stream_format=fmt)
    stream.write(pcm)
    stream.close()  # sinaliza fim do áudio; sem isso o recognize_once espera até o timeout

    audio_config = speechsdk.audio.AudioConfig(stream=stream)
    recognizer = speechsdk.SpeechRecognizer(speech_config=cfg, audio_config=audio_config)

    pa_config = speechsdk.PronunciationAssessmentConfig(
        reference_text=reference_text,
        grading_system=speechsdk.PronunciationAssessmentGradingSystem.HundredMark,
        granularity=speechsdk.PronunciationAssessmentGranularity.Phoneme,
        enable_miscue=True,
    )
    pa_config.phoneme_alphabet = "IPA"
    pa_config.enable_prosody_assessment()
    pa_config.apply_to(recognizer)

    result = recognizer.recognize_once()

    if result.reason == speechsdk.ResultReason.NoMatch:
        return {"recognized": False, "text": "", "raw": None,
                "error": "Não entendi nada. Fale mais perto do microfone."}
    if result.reason == speechsdk.ResultReason.Canceled:
        details = result.cancellation_details
        log.error("Azure cancelou: %s %s", details.reason, details.error_details)
        return {"recognized": False, "text": "", "raw": None,
                "error": f"Azure cancelou o reconhecimento: {details.error_details}"}

    raw = json.loads(result.properties.get(speechsdk.PropertyId.SpeechServiceResponse_JsonResult))
    return {"recognized": True, "text": result.text, "raw": raw, "error": None}


# ---------------------------------------------------------------------------
# TTS com visemas (para o boneco mexer a boca)
# ---------------------------------------------------------------------------
# Vozes usadas quando a voz principal é monolíngue e o trecho é do outro idioma.
DEFAULT_PT_VOICE = "pt-BR-AntonioNeural"
DEFAULT_EN_VOICE = "en-US-GuyNeural"
SOUND_PAUSE_MS = 350   # silêncio antes e depois de um som isolado
SOUND_GAP_MS = 250     # silêncio entre as repetições do som
SOUND_REPEAT = 2       # quantas vezes o som isolado é dito


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def voice_locale(voice: str) -> str:
    """'pt-BR-MacerioMultilingualNeural' -> 'pt-BR'; 'en-US-Ava:DragonHDLatestNeural' -> 'en-US'."""
    p = voice.split("-")
    return f"{p[0]}-{p[1]}" if len(p) >= 3 else "pt-BR"


def accepts_lang_tag(voice: str) -> bool:
    """Só vozes multilíngues (e HD) obedecem <lang xml:lang>. As outras ignoram, por design do Azure."""
    return "Multilingual" in voice or "DragonHD" in voice


def _accepts_style(voice: str) -> bool:
    # Multilíngues e DragonHD (não-Omni) não aceitam os estilos que usamos (express-as).
    return not ("Multilingual" in voice or ("DragonHD" in voice and "Omni" not in voice))


def _voice_for(lang: str, main: str) -> str:
    """Qual voz lê este trecho."""
    if lang == speech.SOUND:
        custom = getattr(settings, "AZURE_TTS_VOICE_SOUND", "")
        if custom:
            return custom
        lang = speech.EN
    explicit = getattr(settings, "AZURE_TTS_VOICE_PT" if lang == speech.PT else "AZURE_TTS_VOICE_EN", "")
    if explicit:
        return explicit
    if accepts_lang_tag(main) or voice_locale(main).lower().startswith(lang):
        return main
    return DEFAULT_PT_VOICE if lang == speech.PT else DEFAULT_EN_VOICE


def _in_lang(xml: str, locale: str, voice: str) -> str:
    return f'<lang xml:lang="{locale}">{xml}</lang>' if accepts_lang_tag(voice) else xml


def _segment_xml(seg: dict, voice: str) -> str:
    locale = speech.LOCALE[seg["lang"]]
    if seg["lang"] != speech.SOUND:
        return _in_lang(_esc(seg["text"]), locale, voice) + " "
    # Som isolado: pausa, o som (pelo IPA), pausa, o som de novo, pausa.
    one = _in_lang(f'<phoneme alphabet="ipa" ph="{_esc(seg["ipa"])}">{_esc(seg["text"])}</phoneme>', locale, voice)
    gap = f'<break time="{SOUND_GAP_MS}ms"/>'
    pause = f'<break time="{SOUND_PAUSE_MS}ms"/>'
    return pause + gap.join([one] * SOUND_REPEAT) + pause


def build_ssml(segs: list[dict], voice: str, style: str | None = None) -> str:
    """
    Cada trecho do LLM vira SSML com o idioma que ele mandou:
      - voz multilíngue/HD: uma voz só, cada trecho em <lang xml:lang="pt-BR|en-US">;
      - voz monolíngue: cada trecho vai para uma voz nativa daquele idioma.
    O xml:lang do <speak> é o locale principal da voz (exigência do Azure).
    """
    blocks: list[list[str]] = []  # [voz, xml] — trechos seguidos da mesma voz ficam juntos
    for seg in segs:
        v = _voice_for(seg["lang"], voice)
        xml = _segment_xml(seg, v)
        if blocks and blocks[-1][0] == v:
            blocks[-1][1] += xml
        else:
            blocks.append([v, xml])

    body = ""
    for v, xml in blocks:
        s = style if v == voice and _accepts_style(v) else None
        inner = f'<mstts:express-as style="{s}">{xml}</mstts:express-as>' if s else xml
        body += f'<voice name="{v}">{inner}</voice>'
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        f'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="{voice_locale(voice)}">{body}</speak>'
    )


def synthesize_with_visemes(segs, voice: str | None = None, style: str | None = None) -> dict:
    """
    Sintetiza os trechos do LLM (lista de speech.normalize; uma string vira português)
    e devolve {"audio_mp3": bytes, "visemes": [[ms, viseme_id], ...]}.
    O viseme_id é um inteiro de 0 a 21 (tabela do Azure). O front mapeia para bocas.
    `style` usa mstts:express-as ("cheerful", "friendly", "excited"...) e só vale em vozes
    que suportam estilos (Davis, Jenny, Guy, Aria...). Vozes HD não emitem visemas: a lista
    volta vazia e o front anima a boca pelo volume do áudio.
    """
    cfg = _speech_config()
    voice = voice or settings.AZURE_TTS_VOICE
    cfg.speech_synthesis_voice_name = voice
    cfg.set_speech_synthesis_output_format(
        speechsdk.SpeechSynthesisOutputFormat.Audio16Khz32KBitRateMonoMp3
    )

    synthesizer = speechsdk.SpeechSynthesizer(speech_config=cfg, audio_config=None)
    visemes: list[list[int]] = []

    def on_viseme(evt: speechsdk.SpeechSynthesisVisemeEventArgs):
        # audio_offset vem em ticks de 100 ns -> ms
        visemes.append([int(evt.audio_offset / 10_000), int(evt.viseme_id)])

    synthesizer.viseme_received.connect(on_viseme)

    ssml = build_ssml(speech.normalize(segs), voice, style)
    log.debug("SSML: %s", ssml)

    result = synthesizer.speak_ssml_async(ssml).get()
    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        details = getattr(result, "cancellation_details", None)
        raise RuntimeError(f"TTS falhou: {details.error_details if details else result.reason}")

    return {"audio_mp3": bytes(result.audio_data), "visemes": visemes}
