"""
O professor. Usa qualquer API compatível com OpenAI (Groq/Llama por padrão).
Sempre responde em JSON com: reply, focus, mood. O fluxo (o que repetir) é decidido pelo coach.
"""
import json
import logging

from django.conf import settings
from openai import OpenAI

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
{sass}

REGRAS:
- Responda em português do Brasil, com as palavras/sons em inglês quando for corrigir.
- Fale como gente, curto: no máximo 3 frases no campo "reply". É uma conversa por voz.
- Cite NO MÁXIMO 2 erros por vez, o pior primeiro. Se não houver erro, elogie (do seu jeito) e suba a dificuldade.
- Ao corrigir um som, explique COMO fazer com a boca (use as dicas fornecidas), não só diga que está errado.
- Use o histórico: se o aluno repete o mesmo erro, comente isso.
- Quem decide o que o aluno repete a seguir é o SISTEMA (vem em "O que fazer agora"). Você NÃO escolhe frases
  novas nem muda o alvo. Sempre termine dizendo claramente o que ele deve falar (a palavra ou a frase indicada).
- Toda palavra, som ou frase em INGLÊS dentro de "reply" vai entre colchetes, ex.: "Não é [fink], é [think]. Repete: [I think the weather is nice]."
  Português fica fora dos colchetes. Isso controla a pronúncia da voz.
- Nada de emojis.

FORMATO: responda SOMENTE um JSON válido, sem markdown:
{{"reply": "texto falado pelo professor", "focus": "som em foco, ex.: 'th' (ou null)", "mood": "um de: happy, sassy, shocked, neutral"}}
"""

MAX_HISTORY_TURNS = 6


def _client() -> OpenAI:
    if not settings.LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY não configurada.")
    return OpenAI(base_url=settings.LLM_BASE_URL, api_key=settings.LLM_API_KEY)


def build_user_message(reference: str, summary: dict, instruction: str = "") -> str:
    if not summary.get("recognized"):
        body = (
            f"Alvo pedido: \"{reference}\"\n"
            f"Resultado: o sistema não reconheceu nada ({summary.get('error')})."
        )
    else:
        s = summary["scores"]
        body = (
            f"Alvo pedido: \"{reference}\"\n"
            f"O aluno disse: \"{summary['transcript']}\"\n"
            f"Notas (0-100): pronúncia geral {s['overall']}, precisão {s['accuracy']}, "
            f"fluência {s['fluency']}, completude {s['completeness']}"
            + (f", prosódia {s['prosody']}" if s.get("prosody") is not None else "")
            + "\nProblemas (piores primeiro):\n"
            + summary["issues_text"]
        )
    if instruction:
        body += f"\n\nO que fazer agora: {instruction}"
    return body


def _complete(client: OpenAI, messages: list[dict]) -> str:
    """
    Chama o modelo pedindo JSON. Modelos de raciocínio (gpt-oss, deepseek-r1, o-series)
    gastam tokens "pensando" antes de responder, então o limite precisa ser folgado e
    o raciocínio, curto. Se o provedor recusar algum parâmetro, tenta de novo sem ele.
    """
    base = dict(model=settings.LLM_MODEL, messages=messages, temperature=0.8, max_tokens=1500)
    attempts = [
        {**base, "response_format": {"type": "json_object"}, "reasoning_effort": "low"},
        {**base, "response_format": {"type": "json_object"}},
        base,
    ]
    last_err = None
    for kwargs in attempts:
        try:
            resp = client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content or "{}"
        except Exception as e:  # parâmetro não suportado, json_validate_failed, etc.
            last_err = e
            log.warning("LLM falhou com %s: %s", [k for k in kwargs if k not in base], str(e)[:200])
    raise last_err


def _parse_json(content: str) -> dict:
    """Aceita JSON puro, JSON dentro de ```fences``` ou JSON no meio de texto."""
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
    log.warning("LLM não devolveu JSON: %s", content[:200])
    return {"reply": text[:300]}


def teacher_reply(reference: str, summary: dict, history: list[dict], instruction: str = "") -> dict:
    """
    history: lista de {"role": "user"|"assistant", "content": str} das últimas trocas.
    instruction: decisão do coach (o que o professor deve fazer agora).
    Devolve dict com reply, focus, mood.
    """
    client = _client()
    sass = SASS.get(settings.TEACHER_SASS_LEVEL, SASS[2])
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(sass=sass)}]
    messages += history[-MAX_HISTORY_TURNS * 2:]
    user_msg = build_user_message(reference, summary, instruction)
    messages.append({"role": "user", "content": user_msg})

    content = _complete(client, messages)
    data = _parse_json(content)

    return {
        "reply": (data.get("reply") or "Hmm. Tenta de novo.").strip(),
        "focus": data.get("focus"),
        "mood": data.get("mood") if data.get("mood") in ("happy", "sassy", "shocked", "neutral") else "neutral",
        "_user_message": user_msg,
    }


def opening_line(first_sentence: str) -> dict:
    """Primeira fala, sem gastar LLM."""
    return {
        "reply": f"Beleza, vamos ver se você fala inglês ou só acha que fala. Repete essa frase aí: [{first_sentence}]",
        "focus": None,
        "mood": "sassy",
    }
