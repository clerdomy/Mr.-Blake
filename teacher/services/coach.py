"""
Controle de fluxo da aula (máquina de estados). O LLM só narra; quem decide
se o aluno passa, trava numa palavra ou avança é este módulo.

Estados (guardados na sessão):
  mode      "sentence" (repetir a frase inteira) | "word" (treinar uma palavra)
  idx       índice da frase atual no currículo
  word      palavra em treino (só em mode="word")
  s_tries   tentativas na frase atual
  w_tries   tentativas na palavra atual
  done      frases concluídas
"""
import re

from django.conf import settings

# Limiares (0-100)
PASS_SENTENCE = 75   # nota geral mínima para a frase passar
WORD_BAD = 60        # palavra abaixo disso "reprova" a frase
PASS_WORD = 70       # nota mínima da palavra no modo treino
MAX_WORD_TRIES = 4   # depois disso o professor libera e volta pra frase (0 = nunca libera)
MAX_SENTENCE_TRIES = 6  # depois disso avança mesmo assim (0 = nunca)

# Currículo: frases pensadas para os erros clássicos de brasileiros.
SENTENCES = [
    "I think the weather is really nice today.",
    "This is the third thing I thought about.",
    "She sells sheep and ships at the beach.",
    "The world is very hard for the early bird.",
    "I would like a cup of coffee with milk.",
    "My brother works with three other people.",
    "We watched the whole movie with our friends.",
    "Please close the door and sit down.",
    "He said that he was very busy yesterday.",
    "They live in a big house near the river.",
    "I usually walk to work in the morning.",
    "Can you tell me where the bathroom is?",
    "The teacher asked a difficult question.",
    "Both of them thought the food was delicious.",
    "It was worth every single penny.",
    "I need to buy some vegetables and bread.",
    "Her birthday party was last Thursday night.",
    "We should leave before the rain starts.",
    "Those are the best shoes I have ever seen.",
    "Thank you very much for everything.",
]


def new_state() -> dict:
    return {"mode": "sentence", "idx": 0, "word": None, "s_tries": 0, "w_tries": 0, "done": 0}


def current_sentence(state: dict) -> str:
    return SENTENCES[state["idx"] % len(SENTENCES)]


def reference_for(state: dict) -> str:
    """Texto que o aluno deve falar agora (frase inteira ou só a palavra)."""
    return state["word"] if state["mode"] == "word" and state["word"] else current_sentence(state)


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


def evaluate(state: dict, summary: dict) -> tuple[dict, str, str]:
    """
    Aplica o resultado da avaliação ao estado.
    Devolve (novo_estado, evento, instrucao_para_o_professor).
    Eventos: no_speech, sentence_pass, enter_word, sentence_retry, sentence_giveup,
             word_pass, word_retry, word_giveup
    """
    st = dict(state)
    sentence = current_sentence(st)

    if not summary.get("recognized"):
        alvo = reference_for(st)
        return st, "no_speech", (
            f"O sistema não entendeu nada do que o aluno falou. Zoe de leve e peça para "
            f"repetir de novo, mais perto do microfone: \"{alvo}\"."
        )

    # ---------------- modo palavra ----------------
    if st["mode"] == "word":
        st["w_tries"] += 1
        target = _norm(st["word"])
        match = next((w for w in summary["words"] if _norm(w["word"]) == target), None)
        ok = bool(match) and match["error"] == "None" and match["score"] >= PASS_WORD

        if ok:
            st.update(mode="sentence", word=None, w_tries=0)
            return st, "word_pass", (
                f"O aluno finalmente acertou a palavra '{match['word']}' (nota {match['score']}). "
                f"Comemore do seu jeito e mande ele voltar para a frase inteira: \"{sentence}\"."
            )
        if MAX_WORD_TRIES and st["w_tries"] >= MAX_WORD_TRIES:
            word = st["word"]
            st.update(mode="sentence", word=None, w_tries=0)
            return st, "word_giveup", (
                f"O aluno tentou '{word}' {MAX_WORD_TRIES} vezes e ainda não saiu perfeito. "
                f"Libere por enquanto (com deboche, mas sem humilhar), diga que vão voltar nisso depois, "
                f"e peça a frase inteira: \"{sentence}\"."
            )
        nota = f"nota {match['score']}" if match else "nem foi reconhecida"
        return st, "word_retry", (
            f"Ainda não. Tentativa {st['w_tries']} na palavra '{st['word']}' ({nota}). "
            f"Explique de novo COMO fazer o som com a boca (use as dicas dos fonemas), talvez de um jeito diferente, "
            f"e peça para repetir SÓ a palavra: \"{st['word']}\"."
        )

    # ---------------- modo frase ----------------
    st["s_tries"] += 1
    bad = _bad_words(summary)
    overall = summary["scores"]["overall"]
    passed = overall >= PASS_SENTENCE and not bad

    if passed:
        st.update(idx=st["idx"] + 1, done=st["done"] + 1, s_tries=0)
        nxt = current_sentence(st)
        return st, "sentence_pass", (
            f"O aluno acertou a frase inteira (nota geral {overall}). Elogie do seu jeito "
            f"(pode fingir que está surpreso) e apresente a próxima frase: \"{nxt}\"."
        )

    if MAX_SENTENCE_TRIES and st["s_tries"] >= MAX_SENTENCE_TRIES:
        st.update(idx=st["idx"] + 1, done=st["done"] + 1, s_tries=0, mode="sentence", word=None)
        nxt = current_sentence(st)
        return st, "sentence_giveup", (
            f"O aluno tentou essa frase {MAX_SENTENCE_TRIES} vezes. Deixe passar por hoje, com deboche leve, "
            f"e apresente a próxima frase: \"{nxt}\"."
        )

    if bad:
        worst = bad[0]
        st.update(mode="word", word=worst["word"], w_tries=0)
        motivo = "pulou a palavra" if worst["error"] == "Omission" else f"nota {worst['score']}"
        return st, "enter_word", (
            f"A frase não passou. A pior palavra foi '{worst['word']}' ({motivo}). "
            f"Diga que vão parar nela: explique COMO fazer o som com a boca (use as dicas dos fonemas) "
            f"e peça para repetir SÓ essa palavra: \"{worst['word']}\". Não peça a frase inteira agora."
        )

    # nenhuma palavra ruim mas nota geral baixa (fluência/prosódia)
    return st, "sentence_retry", (
        f"As palavras estão ok, mas a frase ficou travada/robótica (nota geral {overall}). "
        f"Peça para repetir a frase inteira com mais naturalidade: \"{sentence}\"."
    )


def progress(state: dict) -> dict:
    return {
        "done": state["done"],
        "total": len(SENTENCES),
        "sentence_tries": state["s_tries"],
        "word_tries": state["w_tries"],
    }
