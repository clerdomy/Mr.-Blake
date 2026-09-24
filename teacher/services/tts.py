"""
Voz do professor. Escolhe o provedor por TTS_PROVIDER:
  - "azure"  -> Azure TTS com visemas (boca sincronizada por fonema)
  - "openai" -> OpenAI gpt-4o-mini-tts (voz mais natural, aceita instrução de tom;
                sem visemas: o front anima a boca pelo volume do áudio)
Sempre devolve {"audio_mp3": bytes, "visemes": [[ms, id], ...]} (lista vazia na OpenAI).
"""
import logging

from django.conf import settings

log = logging.getLogger(__name__)

# Instrução de tom por humor (só a OpenAI usa isso)
OPENAI_TONE = {
    "sassy": "Fale em português do Brasil, como um professor sarcástico e debochado, mas simpático. "
             "Ritmo natural de conversa, com ironia leve. Palavras em inglês com pronúncia americana nativa.",
    "happy": "Fale em português do Brasil, animado e orgulhoso do aluno, com energia. "
             "Palavras em inglês com pronúncia americana nativa.",
    "shocked": "Fale em português do Brasil, chocado e dramático, exagerando a surpresa de forma cômica. "
               "Palavras em inglês com pronúncia americana nativa.",
    "neutral": "Fale em português do Brasil, natural e claro, como um professor amigável. "
               "Palavras em inglês com pronúncia americana nativa.",
}


def synthesize(text: str, mood: str = "neutral") -> dict:
    provider = (settings.TTS_PROVIDER or "azure").lower()
    if provider == "openai":
        return _openai(text, mood)
    return _azure(text, mood)


def _azure(text: str, mood: str) -> dict:
    from . import azure_speech

    style = {"happy": "cheerful", "sassy": "friendly", "shocked": "excited"}.get(mood)
    try:
        return azure_speech.synthesize_with_visemes(text, style=style)
    except Exception:
        log.exception("TTS Azure com estilo falhou, tentando sem estilo")
        return azure_speech.synthesize_with_visemes(text)


def _openai(text: str, mood: str) -> dict:
    from openai import OpenAI

    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY não configurada (TTS_PROVIDER=openai).")
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    resp = client.audio.speech.create(
        model=settings.OPENAI_TTS_MODEL,
        voice=settings.OPENAI_TTS_VOICE,
        input=text,
        instructions=OPENAI_TONE.get(mood, OPENAI_TONE["neutral"]),
        response_format="mp3",
    )
    return {"audio_mp3": resp.content, "visemes": []}
