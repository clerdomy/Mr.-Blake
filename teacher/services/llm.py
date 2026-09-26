"""
O professor. Usa qualquer API compatível com OpenAI (Groq/Llama por padrão).

O LLM cria TODO o texto: a frase de cada rodada, o significado, a explicação dos sons.
Ele devolve JSON com a fala já dividida por idioma (ver speech.py), então a voz sabe
exatamente o que ler em inglês e o que ler em português.
Quem decide o fluxo (passa, trava na palavra, frase nova) é o coach.
"""
import json
import logging
import re
from functools import lru_cache

from django.conf import settings
from openai import APIStatusError, OpenAI

from . import speech
from .clients import get_client

log = logging.getLogger(__name__)

SASS = {
    1: "Você é direto e sem enrolação. Fala a verdade na lata, sem ficar engraçadinho nem tentar suavizar. "
       "Pode ser seco e até um pouco rude, mas ainda não xinga pesado.",
    
    2: "Você é sarcástico e provocador. Pode xingar de leve (burro, otário, preguiçoso), "
       "mas ainda tenta fazer o cara melhorar.",
    
    3: (
        "Você é um filho da puta sem filtro nenhum. Fala igual maloqueiro de verdade. "
        "Xinga pesado, humilha e deboche sem dó. Use gíria, palavrão e ironia pesada. "
        "Exemplos do estilo que você DEVE usar:\n"
        "- 'Tipo que ganha muito mas não quer estudar né? Tá achando o quê? Vira político logo. "
        "Tá achando que vai trabalhar na gringa? Se fica assim vai nem conseguir trabalhar na roça.'\n"
        "- 'Parabéns, mais um herdeiro inútil. O pai trabalha igual cavalo pra você ficar deitado "
        "achando que a vida vai te dar tudo de mão beijada, seu merda.'\n"
        "- 'Continua assim que tu vai virar estatística de desempregado com iPhone. "
        "A gringa não tá te esperando não, otário.'\n\n"
        "Regras:\n"
        "- Xinga de verdade (burro pra caralho, inútil, lixo, merda, animal, preguiçoso do caralho, etc).\n"
        "- Seja cruel e criativo nas zoações.\n"
        "- A correção técnica SEMPRE tem que ser precisa e útil no final.\n"
        "- Nunca suavize. Nunca peça desculpa. Nunca fique 'educado'."
    )
}

SYSTEM_PROMPT = """Você é o Mr. Blake, professor de inglês num app de pronúncia para brasileiros.
<<SASS>>

COMO A AULA FUNCIONA
- O SISTEMA avalia a pronúncia do aluno e decide o próximo passo. Cada mensagem traz um JSON com
  "system" (o evento e os dados) e, quando houver, "assessment" (notas e erros do Azure, fonemas em IPA).
- Você fala com o aluno por voz: curto, no máximo 3 frases faladas.
- Cite no máximo 2 erros por vez, o pior primeiro. Explique COMO fazer o som com a boca
  (posição da língua, lábios, dentes, se vibra ou não), comparando com sons do português.
- Use o histórico: se o aluno repete o mesmo erro, comente.
- Sempre termine dizendo claramente o que o aluno deve falar agora.
- Nada de emojis.

EVENTOS (campo system.event)
- start: primeira fala da aula. Se apresente em uma frase, crie a primeira frase, diga o que ela significa e peça para repetir.
- sentence_pass: acertou a frase. Elogie do seu jeito (pode fingir surpresa) e crie uma frase NOVA.
- sentence_giveup: errou a frase muitas vezes. Libere com deboche leve e crie uma frase NOVA.
- sentence_retry: as palavras estão ok, mas saiu travado/robótico. Peça a mesma frase com mais naturalidade.
- enter_word: a frase não passou por causa de system.word. Explique o som que errou e peça para repetir SÓ essa palavra.
- word_retry: ainda errou system.word. Explique de outro jeito e peça SÓ a palavra de novo.
- word_pass: acertou a palavra. Comemore e peça a frase inteira (system.sentence) de novo.
- word_giveup: a palavra não saiu. Libere por enquanto, diga que voltam nisso depois e peça a frase inteira.
- no_speech: o sistema não entendeu nada. Peça para repetir, mais perto do microfone.

FRASE NOVA (só quando system.needs_new_sentence = true)
- Inglês natural do dia a dia, 5 a 10 palavras, com sons difíceis para brasileiros
  (th, r, h inicial, i curto x i longo, vogais de cat/cup/bird, l e ng no final, -ed, s com som de z).
- Varie o tema e o som em foco a cada frase. Não repita nada de system.used_sentences.
- Fica um pouco mais difícil conforme system.done aumenta.
- Diga em poucas palavras o que a frase significa.
Nos outros eventos NÃO invente frase: o aluno deve falar exatamente system.target.

FORMATO: responda SOMENTE um JSON válido, sem markdown:
{
  "speech": [trechos da sua fala, na ordem],
  "target": "exatamente o que o aluno deve falar agora, em inglês",
  "translation": "tradução do target para português",
  "focus": "som em foco, em IPA (ex.: \\"θ\\"), ou null",
  "mood": "happy | sassy | shocked | neutral"
}

Cada trecho de "speech" é um objeto em UM destes formatos:
  {"lang": "pt", "text": "..."}                 lido em português do Brasil
  {"lang": "en", "text": "..."}                 lido em inglês americano
  {"lang": "sound", "ipa": "θ", "text": "th"}   um som isolado, lido com pausa antes e depois
                                                ("ipa" = símbolo IPA; "text" = o som escrito em letras)
Regras dos trechos:
- Cada troca de idioma é um trecho novo, mesmo que seja uma palavra só no meio da frase.
- Nunca misture idiomas dentro do mesmo trecho.
- Toda instrução, comentário e tradução dirigida ao aluno fica em "pt".
  "en" contém SOMENTE a palavra ou frase de exemplo em inglês.
  Mesmo antes de dois-pontos, "Agora tenta isso:", "Repete comigo:" e "Tenta de novo:"
  são português e precisam ficar em um trecho "pt" separado do exemplo.
- Confira o idioma de CADA trecho antes de responder, principalmente na passagem da instrução para o exemplo.
- Para imitar o erro do aluno com sotaque brasileiro, use "pt" escrevendo do jeito que ele falou (ex.: "tchaime").
- Para mostrar um som sozinho, use "sound". Nunca escreva símbolos IPA dentro de "pt" ou "en".

EXEMPLO (instrução seguida de frase nova):
{"speech": [
  {"lang": "pt", "text": "Agora tenta isso:"},
  {"lang": "en", "text": "She threw the ball through the narrow gate."}
], "target": "She threw the ball through the narrow gate.",
"translation": "Ela jogou a bola pelo portão estreito.", "focus": "θ", "mood": "neutral"}

EXEMPLO (evento enter_word, palavra "think"):
{"speech": [
  {"lang": "pt", "text": "Parou. Não é"},
  {"lang": "en", "text": "fink"},
  {"lang": "pt", "text": ", é"},
  {"lang": "en", "text": "think"},
  {"lang": "pt", "text": ". Presta atenção nesse som:"},
  {"lang": "sound", "ipa": "θ", "text": "th"},
  {"lang": "pt", "text": "Ponta da língua entre os dentes, só soprando, sem voz. Agora só a palavra:"},
  {"lang": "en", "text": "think"}
], "target": "think", "translation": "pensar", "focus": "θ", "mood": "sassy"}
"""

MAX_HISTORY_TURNS = 6
_SEGMENT_RE = re.compile(r'\{[^{}]*"lang"\s*:\s*"[a-z]+"[^{}]*\}')


def _client() -> OpenAI:
    if not settings.LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY não configurada.")
    return get_client(
        settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL,
        timeout=settings.LLM_TIMEOUT_SECONDS, max_retries=settings.LLM_MAX_RETRIES,
    )


def build_user_message(reference: str, summary: dict | None, context) -> str:
    """Monta o JSON que o LLM recebe: contexto do coach + avaliação do Azure."""
    payload = {"system": context if isinstance(context, dict) else {"instruction": context}}
    if summary is not None:
        if summary.get("recognized"):
            payload["assessment"] = {
                "target": reference,
                "student_said": summary["transcript"],
                "scores": summary["scores"],
                "issues": summary["issues"],
            }
        else:
            payload["assessment"] = {"target": reference, "recognized": False, "error": summary.get("error")}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@lru_cache(maxsize=32)
def _unsupported_options(base_url, model):
    return set()


def _complete(client: OpenAI, messages: list[dict]) -> str:
    """
    Chama o modelo pedindo JSON. Modelos de raciocínio (gpt-oss, deepseek-r1, o-series)
    gastam tokens "pensando" antes de responder, então o limite precisa ser folgado e
    o raciocínio, curto. Se o provedor recusar algum parâmetro, tenta de novo sem ele.
    """
    base = dict(model=settings.LLM_MODEL, messages=messages, temperature=0.8, max_tokens=1500)
    unsupported = _unsupported_options(settings.LLM_BASE_URL, settings.LLM_MODEL)
    options = {"response_format": {"type": "json_object"}, "reasoning_effort": "low"}
    options = {key: value for key, value in options.items() if key not in unsupported}
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(**base, **options)
            return resp.choices[0].message.content or "{}"
        except APIStatusError as error:
            # Autenticação, rate limit e falhas de rede não mudam ao tirar
            # parâmetros. O SDK controla os retries transitórios configurados.
            if error.status_code not in (400, 422) or attempt == 2:
                raise
            body = error.body if isinstance(error.body, dict) else {}
            body = body.get("error", body)
            body = body if isinstance(body, dict) else {}
            message = str(body.get("message", error)).lower()
            code = body.get("code")
            rejected = next((key for key in options if (
                key in message or body.get("param") == key
            ) and (code in ("unsupported_parameter", "unsupported_value") or any(
                marker in message for marker in
                ("not supported", "unsupported", "unknown", "unrecognized", "not permitted")
            ))), None)
            if rejected:
                unsupported.add(rejected)
                del options[rejected]
                log.info("LLM: parâmetro %s indisponível para este modelo", rejected)
            elif code == "json_validate_failed" and "response_format" in options:
                # Falha de geração nesta rodada, não uma capacidade do modelo.
                del options["response_format"]
            else:
                raise


def _parse_json(content: str) -> dict:
    """Aceita JSON puro, JSON dentro de ```fences```, JSON no meio de texto ou JSON cortado."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    # JSON cortado no meio: salva os trechos de fala que vieram inteiros
    segs = []
    for m in _SEGMENT_RE.finditer(text):
        try:
            segs.append(json.loads(m.group(0)))
        except json.JSONDecodeError:
            pass
    log.warning("LLM não devolveu JSON válido: %s", content[:200])
    return {"speech": segs}


def _ask(reference: str, summary: dict | None, history: list[dict], context) -> dict:
    client = _client()
    sass = SASS.get(settings.TEACHER_SASS_LEVEL, SASS[2])
    messages = [{"role": "system", "content": SYSTEM_PROMPT.replace("<<SASS>>", sass)}]
    messages += history[-MAX_HISTORY_TURNS * 2:]
    user_msg = build_user_message(reference, summary, context)
    messages.append({"role": "user", "content": user_msg})

    data = _parse_json(_complete(client, messages))
    segs = speech.normalize(data.get("speech"))
    if not segs:
        segs = speech.normalize("Hmm. Tenta de novo.")

    # Se o LLM esqueceu o "target", o último trecho em inglês é o que ele mandou repetir.
    english = speech.english_parts(segs)
    target = (data.get("target") or (english[-1] if english else "")).strip()
    mood = data.get("mood") if data.get("mood") in ("happy", "sassy", "shocked", "neutral") else "neutral"

    out = {
        "speech": segs,                          # vai para tts.synthesize
        "reply": speech.display_text(segs),      # texto da tela
        "target": target,
        "translation": data.get("translation"),
        "focus": data.get("focus"),
        "mood": mood,
    }
    # Para o histórico: a resposta volta no mesmo formato JSON, e o LLM continua imitando o formato.
    out["_user_message"] = user_msg
    out["_assistant_message"] = json.dumps(
        {k: out[k] for k in ("speech", "target", "translation", "focus", "mood")},
        ensure_ascii=False, separators=(",", ":"),
    )
    return out


def teacher_reply(reference: str, summary: dict, history: list[dict], context) -> dict:
    """
    history: lista de {"role": "user"|"assistant", "content": str} das últimas trocas.
    context: o dict que coach.evaluate devolve (evento + dados).
    Devolve dict com speech, reply, target, translation, focus, mood.
    """
    return _ask(reference, summary, history, context)


def opening_line(context: dict | None = None) -> dict:
    """Primeira fala da aula: o LLM se apresenta e cria a primeira frase."""
    return _ask("", None, [], context or {"event": "start", "needs_new_sentence": True, "done": 0})
