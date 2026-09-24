"""
Azure Speech: avaliação de pronúncia (Pronunciation Assessment) e TTS com visemas.
Docs: https://learn.microsoft.com/azure/ai-services/speech-service/how-to-pronunciation-assessment
"""
import io
import re
import json
import logging
import wave

import azure.cognitiveservices.speech as speechsdk
from django.conf import settings

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
_EN_MARK = re.compile(r"\[([^\[\]]+)\]")


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_mixed_language_ssml(text: str) -> str:
    """
    O professor marca inglês entre colchetes: "Não é [fink], é [think]."
    Vira: <lang xml:lang="pt-BR">Não é </lang><lang xml:lang="en-US">fink</lang>...
    Vozes multilíngues/HD respeitam <lang>; vozes comuns ignoram sem erro.
    """
    parts = []
    pos = 0
    for m in _EN_MARK.finditer(text):
        pt = text[pos:m.start()]
        if pt.strip():
            parts.append(f'<lang xml:lang="pt-BR">{_esc(pt)}</lang>')
        elif pt:
            parts.append(_esc(pt))
        parts.append(f'<lang xml:lang="en-US">{_esc(m.group(1))}</lang>')
        pos = m.end()
    rest = text[pos:]
    if rest.strip():
        parts.append(f'<lang xml:lang="pt-BR">{_esc(rest)}</lang>')
    return "".join(parts) or _esc(text)


def strip_marks(text: str) -> str:
    """Remove os colchetes para exibir na tela."""
    return _EN_MARK.sub(r"\1", text)

def synthesize_with_visemes(text: str, voice: str | None = None, style: str | None = None) -> dict:
    """
    Sintetiza `text` e devolve {"audio_mp3": bytes, "visemes": [[ms, viseme_id], ...]}.
    O viseme_id é um inteiro de 0 a 21 (tabela do Azure). O front mapeia para bocas.
    `style` usa SSML (mstts:express-as), ex.: "cheerful", "sad", "excited", "friendly" —
    só funciona em vozes que suportam estilos (Davis, Jenny, Guy, Aria...).
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

    safe = build_mixed_language_ssml(text)
    # Vozes multilíngues e DragonHD (não-Omni) não aceitam estilos (express-as);
    # usar daria erro e retry. Vozes HD também não emitem visemas: a lista volta
    # vazia e o front anima a boca pelo volume do áudio.
    if "Multilingual" in voice or ("DragonHD" in voice and "Omni" not in voice):
        style = None
    inner = f'<mstts:express-as style="{style}">{safe}</mstts:express-as>' if style else safe
    # xml:lang pt-BR: o texto do professor é em português com palavras em inglês;
    # a voz multilíngue detecta e pronuncia cada trecho no idioma certo.
    ssml = (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="pt-BR">'
        f'<voice name="{voice}">{inner}</voice></speak>'
    )

    result = synthesizer.speak_ssml_async(ssml).get()
    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        details = getattr(result, "cancellation_details", None)
        raise RuntimeError(f"TTS falhou: {details.error_details if details else result.reason}")

    return {"audio_mp3": bytes(result.audio_data), "visemes": visemes}
