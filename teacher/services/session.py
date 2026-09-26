"""Histórico válido e limitado ao tamanho real do cookie assinado."""
import json

from django.conf import settings
from django.core import signing

from .llm import MAX_HISTORY_TURNS

COOKIE_VALUE_BUDGET = 3800  # Reserva espaço para nome e atributos do cookie.


def _cookie_size(data, serializer):
    return len(signing.dumps(
        data, compress=True, serializer=serializer,
        salt="django.contrib.sessions.backends.signed_cookies",
    ))


def save_lesson(session, state, history):
    # Evita repetir a lista de frases antigas em cada mensagem do histórico.
    # O coach já a envia atualizada quando pede uma frase nova.
    compact = []
    pending_user = None
    for message in history[-MAX_HISTORY_TURNS * 2:]:
        content = message["content"]
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            # Cookies de versões antigas podem conter texto/JSON truncado.
            pending_user = None
            continue
        if not isinstance(data, dict):
            pending_user = None
            continue
        if message["role"] == "user":
            context = data.get("system")
            if isinstance(context, dict):
                context.pop("used_sentences", None)
        entry = {"role": message["role"], "content": json.dumps(
            data, ensure_ascii=False, separators=(",", ":"),
        )}
        if message["role"] == "user":
            pending_user = entry
        elif message["role"] == "assistant" and pending_user is not None:
            compact.extend([pending_user, entry])
            pending_user = None

    state = dict(state, used=list(state.get("used", [])))
    payload = {**dict(session.items()), "coach": state, "history": compact}
    if settings.SESSION_ENGINE == "django.contrib.sessions.backends.signed_cookies":
        while _cookie_size(payload, session.serializer) > COOKIE_VALUE_BUDGET:
            if compact:
                # Descarta a troca antiga inteira; nunca corta JSON no meio.
                compact.pop(0)
                while compact and compact[0]["role"] != "user":
                    compact.pop(0)
            elif len(state["used"]) > 1:
                state["used"].pop(0)
            else:
                break
    session["coach"] = state
    session["history"] = compact
