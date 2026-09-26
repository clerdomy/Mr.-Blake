"""
Voz do professor. Escolhe o provedor por TTS_PROVIDER:
  - "azure"  -> Azure TTS com visemas (boca sincronizada por fonema)
  - "openai" -> OpenAI gpt-4o-mini-tts (voz mais natural, aceita instrução de tom;
                sem visemas: o front anima a boca pelo volume do áudio)
Sempre devolve {"audio_mp3": bytes, "visemes": [[ms, id], ...]} (lista vazia na OpenAI).

Recebe a fala em trechos com idioma, do jeito que o LLM devolveu (ver speech.py).
"""
import logging
import hashlib
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings

from . import speech
from .clients import get_client
from .tts_cache import audio_cache

log = logging.getLogger(__name__)

# Tom por humor (só a OpenAI usa isso)
OPENAI_TONE = {
    "sassy": "Professor sarcástico e debochado, mas simpático. Ritmo natural de conversa, com ironia leve.",
    "happy": "Professor animado e orgulhoso do aluno, com energia.",
    "shocked": "Professor chocado e dramático, exagerando a surpresa de forma cômica.",
    "neutral": "Professor amigável, natural e claro.",
}
OPENAI_LANG = {
    speech.PT: "Fale TUDO em português do Brasil, com sotaque brasileiro natural.",
    speech.EN: "Fale TUDO em inglês americano, pronúncia de nativo, clara, como quem mostra o jeito certo.",
}
OPENAI_PCM_RATE = 24_000  # response_format="pcm": 24 kHz, 16-bit, mono
SOUND_PAUSE_MS = 350


def synthesize(segs, mood: str = "neutral") -> dict:
    """`segs`: reply["speech"] do llm (uma string solta é lida como português)."""
    segs = speech.normalize(segs)
    provider = (settings.TTS_PROVIDER or "azure").lower()
    ttl = settings.TTS_CACHE_TTL_SECONDS
    max_bytes = settings.TTS_CACHE_MAX_BYTES
    key = None
    if ttl > 0 and max_bytes > 0:
        fields = ("OPENAI_API_KEY", "OPENAI_TTS_MODEL", "OPENAI_TTS_VOICE", "OPENAI_TTS_SPLIT") if provider == "openai" else (
            "AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION", "AZURE_TTS_VOICE",
            "AZURE_TTS_VOICE_PT", "AZURE_TTS_VOICE_EN", "AZURE_TTS_VOICE_SOUND",
        )
        config = [getattr(settings, name, None) for name in fields]
        key = hashlib.sha256(json.dumps([provider, config, mood, segs], sort_keys=True).encode()).digest()
        cached = audio_cache.get(key)
        if cached is not None:
            return cached
    if provider == "openai":
        result = _openai(_merge_segments(segs), mood)
    else:
        result = _azure(segs, mood)
    if key is not None:
        audio_cache.put(key, result, ttl=ttl, max_bytes=max_bytes)
    return result


def _merge_segments(segs):
    """Uma chamada OpenAI por sequência do mesmo idioma; sons ficam isolados."""
    merged = []
    for seg in segs:
        if merged and seg["lang"] != speech.SOUND and merged[-1]["lang"] == seg["lang"]:
            merged[-1]["text"] = speech.display_text([merged[-1], seg])
        else:
            merged.append(dict(seg))
    return merged


def _azure(segs: list[dict], mood: str) -> dict:
    from . import azure_speech

    style = {"happy": "cheerful", "sassy": "friendly", "shocked": "excited"}.get(mood)
    if not style or "<mstts:express-as" not in azure_speech.build_ssml(segs, settings.AZURE_TTS_VOICE, style):
        return azure_speech.synthesize_with_visemes(segs)
    try:
        return azure_speech.synthesize_with_visemes(segs, style=style)
    except RuntimeError as error:
        if not any(word in str(error).lower() for word in ("style", "express-as")):
            raise
        log.exception("TTS Azure com estilo falhou, tentando sem estilo")
        return azure_speech.synthesize_with_visemes(segs)


# ---------------------------------------------------------------------------
# OpenAI (não tem SSML: cada trecho é sintetizado com a instrução do seu idioma)
# ---------------------------------------------------------------------------
def _openai(segs: list[dict], mood: str) -> dict:
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY não configurada (TTS_PROVIDER=openai).")
    client = get_client(settings.OPENAI_API_KEY, timeout=settings.TTS_TIMEOUT_SECONDS)

    if getattr(settings, "OPENAI_TTS_SPLIT", True) and shutil.which("ffmpeg"):
        try:
            return {"audio_mp3": _openai_split(client, segs, mood), "visemes": []}
        except (OSError, subprocess.SubprocessError):
            log.exception("Conversão do TTS OpenAI falhou; tentando MP3 direto")

    # Plano B (sem ffmpeg): uma chamada só, com a lista do que é inglês na instrução.
    english = "; ".join(f'"{t}"' for t in speech.english_parts(segs)) or "nenhum"
    sounds = "; ".join(f'"{s["text"]}" (som /{s["ipa"]}/)' for s in segs if s["lang"] == speech.SOUND)
    instructions = (
        f"{OPENAI_TONE.get(mood, OPENAI_TONE['neutral'])} {OPENAI_LANG[speech.PT]} "
        f"Exceções, em inglês americano nativo: {english}."
        + (f" Sons isolados, pronuncie só o som, com pausa antes e depois: {sounds}." if sounds else "")
    )
    resp = client.audio.speech.create(
        model=settings.OPENAI_TTS_MODEL,
        voice=settings.OPENAI_TTS_VOICE,
        input=" ".join(s["text"] for s in segs),
        instructions=instructions,
        response_format="mp3",
    )
    return {"audio_mp3": resp.content, "visemes": []}


def _openai_split(client, segs: list[dict], mood: str) -> bytes:
    tone = OPENAI_TONE.get(mood, OPENAI_TONE["neutral"])
    silence = b"\x00\x00" * (OPENAI_PCM_RATE * SOUND_PAUSE_MS // 1000)

    def one(seg: dict) -> bytes:
        if seg["lang"] == speech.SOUND:
            instr = (f"Pronuncie SÓ o som isolado /{seg['ipa']}/ do inglês americano, prolongado, "
                     "como um professor demonstrando. Não diga o nome de letras nem palavras.")
        else:
            instr = f"{tone} {OPENAI_LANG[seg['lang']]}"
        resp = client.audio.speech.create(
            model=settings.OPENAI_TTS_MODEL,
            voice=settings.OPENAI_TTS_VOICE,
            input=seg["text"],
            instructions=instr,
            response_format="pcm",
        )
        audio = resp.content
        return silence + audio + silence if seg["lang"] == speech.SOUND else audio

    with ThreadPoolExecutor(max_workers=4) as pool:  # trechos em paralelo, ordem preservada
        pcm = b"".join(pool.map(one, segs))
    return _pcm_to_mp3(pcm)


def _pcm_to_mp3(pcm: bytes) -> bytes:
    proc = subprocess.run(
        [
            "ffmpeg", "-loglevel", "error",
            "-f", "s16le", "-ar", str(OPENAI_PCM_RATE), "-ac", "1", "-i", "pipe:0",
            "-f", "mp3", "-b:a", "64k", "pipe:1",
        ],
        input=pcm, capture_output=True, check=True, timeout=30,
    )
    return proc.stdout
