import base64
import logging

from django.conf import settings
from django.http import JsonResponse
from django.http import HttpResponse
from django.shortcuts import render
from django.contrib.staticfiles import finders
from django.views.decorators.http import require_POST

from .services import azure_speech, coach, llm, tts
from .services.audio import to_wav_16k
from .services.summarize import summarize

log = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 8 * 1024 * 1024


def index(request):
    return render(request, "teacher/index.html")


def service_worker(request):
    """Serve o sw.js na raiz (o escopo de um service worker é limitado ao caminho dele)."""
    path = finders.find("teacher/sw.js")
    with open(path, "rb") as f:
        resp = HttpResponse(f.read(), content_type="application/javascript")
    resp["Service-Worker-Allowed"] = "/"
    resp["Cache-Control"] = "no-cache"
    return resp


def _speak(text: str, mood: str) -> dict:
    out = tts.synthesize(text, mood)
    return {
        "audio_b64": base64.b64encode(out["audio_mp3"]).decode("ascii"),
        "visemes": out["visemes"],
    }


def _target_payload(state: dict) -> dict:
    return {
        "mode": state["mode"],
        "word": state["word"],
        "next_reference": coach.reference_for(state),
        "sentence": coach.current_sentence(state),
        "progress": coach.progress(state),
    }


@require_POST
def api_start(request):
    """Começa (ou reinicia) a sessão: professor se apresenta e propõe a 1ª frase."""
    state = coach.new_state()
    opening = llm.opening_line(coach.current_sentence(state))
    request.session["coach"] = state
    request.session["history"] = [{"role": "assistant", "content": opening["reply"]}]
    payload = {**opening, **_target_payload(state), "event": "start"}
    payload["reply"] = azure_speech.strip_marks(opening["reply"])
    try:
        payload["speech"] = _speak(opening["reply"], opening["mood"])
    except Exception as e:
        log.exception("TTS falhou")
        payload["speech"] = None
        payload["tts_error"] = str(e)
    return JsonResponse(payload)


@require_POST
def api_assess(request):
    """
    Recebe o áudio do aluno (multipart 'audio'), avalia no Azure contra o alvo atual
    (frase inteira ou palavra em treino), decide o próximo passo (coach) e pede ao
    professor para narrar. Devolve texto + áudio + visemas + próximo alvo.
    """
    audio = request.FILES.get("audio")
    if not audio:
        return JsonResponse({"error": "Envie o campo 'audio'."}, status=400)
    if audio.size > MAX_AUDIO_BYTES:
        return JsonResponse({"error": "Áudio grande demais."}, status=413)

    state = request.session.get("coach")
    if not state:
        return JsonResponse({"error": "Sessão não iniciada. Chame /api/start."}, status=400)
    reference = coach.reference_for(state)

    try:
        wav = to_wav_16k(audio.read())
        assessment = azure_speech.assess_pronunciation(wav, reference)
    except Exception as e:
        log.exception("Falha na avaliação")
        return JsonResponse({"error": f"Falha ao avaliar áudio: {e}"}, status=502)

    summary = summarize(assessment)
    new_state, event, instruction = coach.evaluate(state, summary)

    history = request.session.get("history", [])
    try:
        reply = llm.teacher_reply(reference, summary, history, instruction)
    except Exception as e:
        log.exception("Falha no LLM")
        return JsonResponse({"error": f"Falha no professor: {e}", "summary": summary}, status=502)

    # Histórico compacto (cookie de sessão tem limite de 4 KB)
    history.append({"role": "user", "content": reply.pop("_user_message")[:500]})
    history.append({"role": "assistant", "content": reply["reply"][:300]})
    request.session["history"] = history[-llm.MAX_HISTORY_TURNS * 2:]
    request.session["coach"] = new_state

    payload = {"reference": reference, "summary": summary, "event": event,
               **reply, **_target_payload(new_state)}
    payload["reply"] = azure_speech.strip_marks(reply["reply"])
    try:
        payload["speech"] = _speak(reply["reply"], reply["mood"])
    except Exception as e:
        log.exception("TTS falhou")
        payload["speech"] = None
        payload["tts_error"] = str(e)
    return JsonResponse(payload)


def api_health(request):
    return JsonResponse({
        "ok": True,
        "azure": bool(settings.AZURE_SPEECH_KEY),
        "llm": bool(settings.LLM_API_KEY),
        "model": settings.LLM_MODEL,
        "tts": settings.TTS_PROVIDER,
        "voice": settings.AZURE_TTS_VOICE if settings.TTS_PROVIDER == "azure" else settings.OPENAI_TTS_VOICE,
    })
