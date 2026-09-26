import base64
import logging
import secrets

from django.conf import settings
from django.core import signing
from django.http import JsonResponse
from django.http import HttpResponse
from django.shortcuts import render
from django.contrib.staticfiles import finders
from django.views.decorators.http import require_POST

from .services import azure_speech, coach, llm, tts
from .services.audio import to_wav_16k
from .services.session import save_lesson
from .services.summarize import summarize
from .services.timing import timed_request

log = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 8 * 1024 * 1024
SPEECH_TOKEN_SALT = "teacher.deferred-speech.v1"
SPEECH_TOKEN_MAX_AGE = 180


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


def _speak(segments: list[dict], mood: str) -> dict:
    out = tts.synthesize(segments, mood)
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


def _teacher_response(request, reply, payload):
    if request.POST.get("defer_speech") == "1":
        # Stateless: a próxima chamada pode chegar a outro worker/réplica.
        # Só o servidor pode autorizar o texto a sintetizar para esta aula.
        payload["speech"] = None
        payload["speech_token"] = signing.dumps({
            "lesson": request.session["lesson_id"],
            "segments": reply["speech"], "mood": reply["mood"],
        }, salt=SPEECH_TOKEN_SALT, compress=True)
    else:
        # Mantém o contrato dos clientes que ainda pedem texto e voz juntos.
        try:
            with request.timings.measure("tts"):
                payload["speech"] = _speak(reply["speech"], reply["mood"])
        except Exception as error:
            log.exception("TTS falhou")
            payload["speech"] = None
            payload["tts_error"] = str(error)
    response = JsonResponse(payload)
    response["Cache-Control"] = "no-store"
    return response


@require_POST
@timed_request
def api_start(request):
    """Começa (ou reinicia) a sessão: professor se apresenta e propõe a 1ª frase."""
    state, event, context = coach.start(coach.new_state())
    try:
        with request.timings.measure("llm"):
            opening = llm.opening_line(context)
    except Exception as e:
        log.exception("Falha no LLM ao iniciar")
        return JsonResponse({"error": f"Falha no professor: {e}"}, status=502)
    state = coach.accept_target(state, opening)
    request.session["lesson_id"] = secrets.token_urlsafe(16)
    save_lesson(request.session, state, [
        {"role": "user", "content": opening.pop("_user_message")},
        {"role": "assistant", "content": opening.pop("_assistant_message")},
    ])
    payload = {**opening, **_target_payload(state), "event": event}
    return _teacher_response(request, opening, payload)


@require_POST
@timed_request
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
        with request.timings.measure("audio"):
            wav = to_wav_16k(audio.read())
        with request.timings.measure("assessment"):
            assessment = azure_speech.assess_pronunciation(wav, reference)
    except Exception as e:
        log.exception("Falha na avaliação")
        return JsonResponse({"error": f"Falha ao avaliar áudio: {e}"}, status=502)

    summary = summarize(assessment)
    new_state, event, instruction = coach.evaluate(state, summary)

    history = list(request.session.get("history", []))
    try:
        with request.timings.measure("llm"):
            reply = llm.teacher_reply(reference, summary, history, instruction)
    except Exception as e:
        log.exception("Falha no LLM")
        return JsonResponse({"error": f"Falha no professor: {e}", "summary": summary}, status=502)

    new_state = coach.accept_target(new_state, reply)
    if not request.session.get("lesson_id"):
        # Aulas abertas antes desta versão continuam funcionando.
        request.session["lesson_id"] = secrets.token_urlsafe(16)

    # Limita as trocas e preserva o JSON da fala por idioma no histórico.
    history.append({"role": "user", "content": reply.pop("_user_message")})
    history.append({"role": "assistant", "content": reply.pop("_assistant_message")})
    save_lesson(request.session, new_state, history)

    payload = {"reference": reference, "summary": summary, "event": event,
               **reply, **_target_payload(new_state)}
    return _teacher_response(request, reply, payload)


@require_POST
@timed_request
def api_speech(request):
    """Gera a voz autorizada da aula sem repetir IA, avaliação ou avanço."""
    token = request.POST.get("speech_token", "")
    if not token or len(token) > 16_384:
        return JsonResponse({"error": "Pedido de voz inválido."}, status=400)
    try:
        data = signing.loads(token, salt=SPEECH_TOKEN_SALT, max_age=SPEECH_TOKEN_MAX_AGE)
    except signing.SignatureExpired:
        return JsonResponse({"error": "O pedido de voz expirou."}, status=410)
    except signing.BadSignature:
        return JsonResponse({"error": "Pedido de voz inválido."}, status=400)
    lesson = request.session.get("lesson_id")
    if not lesson or not isinstance(data, dict) or data.get("lesson") != lesson:
        return JsonResponse({"error": "Este áudio pertence a outra aula."}, status=403)
    try:
        with request.timings.measure("tts"):
            voice = _speak(data["segments"], data["mood"])
    except Exception as error:
        log.exception("TTS adiado falhou")
        return JsonResponse({"error": f"Falha ao gerar voz: {error}"}, status=502)
    response = JsonResponse({"speech": voice})
    response["Cache-Control"] = "no-store"
    return response


def api_health(request):
    return JsonResponse({
        "ok": True,
        "azure": bool(settings.AZURE_SPEECH_KEY),
        "llm": bool(settings.LLM_API_KEY),
        "model": settings.LLM_MODEL,
        "tts": settings.TTS_PROVIDER,
        "voice": settings.AZURE_TTS_VOICE if settings.TTS_PROVIDER == "azure" else settings.OPENAI_TTS_VOICE,
    })
