"""
Controle de fluxo da aula (máquina de estados). Este módulo só DECIDE: se o aluno passa,
se trava numa palavra, se precisa de frase nova. Todo texto (frases, explicações,
significados) é criado pelo LLM a partir do contexto que sai daqui.

Estado (guardado na sessão):
  mode      "sentence" (repetir a frase inteira) | "word" (treinar uma palavra)
  sentence  frase atual, criada pelo LLM; None = o LLM precisa criar uma nova
  word      palavra em treino (só em mode="word")
  s_tries   tentativas na frase atual
  w_tries   tentativas na palavra atual
  done      frases concluídas
  used      frases já usadas (para o LLM não repetir)
"""
import re

from django.conf import settings

# Limiares (0-100)
PASS_SENTENCE = 75   # nota geral mínima para a frase passar
WORD_BAD = 60        # palavra abaixo disso "reprova" a frase
PASS_WORD = 70       # nota mínima da palavra no modo treino
MAX_WORD_TRIES = 4   # depois disso o professor libera e volta pra frase (0 = nunca libera)
MAX_SENTENCE_TRIES = 6  # depois disso avança mesmo assim (0 = nunca)
MAX_USED = 30        # quantas frases antigas mandar ao LLM para ele não repetir


def new_state() -> dict:
    return {"mode": "sentence", "sentence": None, "word": None,
            "s_tries": 0, "w_tries": 0, "done": 0, "used": []}


def current_sentence(state: dict) -> str:
    return state.get("sentence") or ""


def reference_for(state: dict) -> str:
    """Texto que o aluno deve falar agora (frase inteira ou só a palavra)."""
    return state["word"] if state["mode"] == "word" and state["word"] else current_sentence(state)


def _context(st: dict, event: str, **data) -> dict:
    """O que o LLM precisa saber para falar. Nenhum texto pronto: só fatos."""
    ctx = {"event": event, "target": reference_for(st) or None, "sentence": st.get("sentence"),
           "done": st["done"], **data}
    if st.get("sentence") is None:
        ctx["needs_new_sentence"] = True
        ctx["used_sentences"] = st.get("used", [])
    return ctx


def start(state: dict) -> tuple[dict, str, dict]:
    """Aluno clicou em começar: o LLM cria a primeira frase."""
    st = dict(state, mode="sentence", sentence=None, word=None, s_tries=0, w_tries=0)
    return st, "start", _context(st, "start")


def accept_target(state: dict, reply: dict) -> dict:
    """
    Chamar depois de cada resposta do LLM. Se o coach pediu frase nova,
    guarda a que o LLM criou. Nos outros casos o alvo continua sendo o do coach.
    """
    st = dict(state)
    target = (reply.get("target") or "").strip()
    if st.get("sentence") is None and target:
        st["sentence"] = target
        st["used"] = (st.get("used", []) + [target])[-MAX_USED:]
    return st


def _norm(w: str) -> str:
    return re.sub(r"[^a-z']", "", w.lower())


def _bad_words(summary: dict) -> list[dict]:
    bad = []
    for w in summary.get("words", []):
        if w["error"] in ("Omission", "Mispronunciation") or (w["error"] != "Insertion" and w["score"] < WORD_BAD):
            bad.append(w)
    # pior primeiro; omissão conta como 0
    bad.sort(key=lambda w: 0 if w["error"] == "Omission" else w["score"])
    return bad


def evaluate(state: dict, summary: dict) -> tuple[dict, str, dict]:
    """
    Aplica o resultado da avaliação ao estado.
    Devolve (novo_estado, evento, contexto_para_o_llm).
    Eventos: no_speech, sentence_pass, enter_word, sentence_retry, sentence_giveup,
             word_pass, word_retry, word_giveup
    """
    st = dict(state)

    if not summary.get("recognized"):
        return st, "no_speech", _context(st, "no_speech")

    # ---------------- modo palavra ----------------
    if st["mode"] == "word":
        st["w_tries"] += 1
        word = st["word"]
        match = next((w for w in summary["words"] if _norm(w["word"]) == _norm(word)), None)
        score = match["score"] if match else None

        if match and match["error"] == "None" and match["score"] >= PASS_WORD:
            st.update(mode="sentence", word=None, w_tries=0)
            return st, "word_pass", _context(st, "word_pass", word=word, score=score)
        if MAX_WORD_TRIES and st["w_tries"] >= MAX_WORD_TRIES:
            st.update(mode="sentence", word=None, w_tries=0)
            return st, "word_giveup", _context(st, "word_giveup", word=word, tries=MAX_WORD_TRIES)
        return st, "word_retry", _context(st, "word_retry", word=word, score=score, tries=st["w_tries"])

    # ---------------- modo frase ----------------
    st["s_tries"] += 1
    bad = _bad_words(summary)
    overall = summary["scores"]["overall"]

    if overall >= PASS_SENTENCE and not bad:
        st.update(sentence=None, done=st["done"] + 1, s_tries=0)
        return st, "sentence_pass", _context(st, "sentence_pass", score=overall)

    if MAX_SENTENCE_TRIES and st["s_tries"] >= MAX_SENTENCE_TRIES:
        st.update(sentence=None, done=st["done"] + 1, s_tries=0, mode="sentence", word=None)
        return st, "sentence_giveup", _context(st, "sentence_giveup", tries=MAX_SENTENCE_TRIES)

    if bad:
        worst = bad[0]
        st.update(mode="word", word=worst["word"], w_tries=0)
        return st, "enter_word", _context(
            st, "enter_word", word=worst["word"],
            omitted=worst["error"] == "Omission", score=worst["score"],
        )

    # nenhuma palavra ruim mas nota geral baixa (fluência/prosódia)
    return st, "sentence_retry", _context(st, "sentence_retry", score=overall)


def progress(state: dict) -> dict:
    return {
        "done": state["done"],
        "total": getattr(settings, "LESSON_LENGTH", 20),
        "sentence_tries": state["s_tries"],
        "word_tries": state["w_tries"],
    }
