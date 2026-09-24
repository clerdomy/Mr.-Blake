"""
Transforma o JSON bruto do Pronunciation Assessment em um resumo compacto,
pensado para (1) o LLM provocar com precisão e (2) o front colorir as palavras.

Estrutura do JSON bruto (resumida):
  raw["NBest"][0]["PronunciationAssessment"] -> AccuracyScore, FluencyScore,
      CompletenessScore, PronScore, ProsodyScore
  raw["NBest"][0]["Words"][i] -> Word, PronunciationAssessment{AccuracyScore, ErrorType},
      Phonemes[j] -> Phoneme (IPA), PronunciationAssessment{AccuracyScore}
"""

PHONEME_THRESHOLD = 60   # fonema abaixo disso é "problema"
WORD_THRESHOLD = 70      # palavra abaixo disso entra na lista de erros
MAX_ISSUES = 4           # quantas palavras problemáticas mandar ao LLM

# Dicas por fonema (IPA) — focadas nos erros clássicos de falantes de português.
PHONEME_TIPS: dict[str, str] = {
    "θ": "TH surdo (think): ponta da língua entre os dentes, só ar, sem voz. Brasileiro tende a falar 'f' ou 't'.",
    "ð": "TH sonoro (this): língua entre os dentes COM voz. Brasileiro tende a falar 'd'.",
    "ɹ": "R inglês: língua enrolada pra trás sem tocar em nada, sem vibrar. Não é o 'r' de 'carro' nem de 'caro'.",
    "r": "R inglês: língua enrolada pra trás sem tocar em nada, sem vibrar.",
    "h": "H aspirado (house): sai ar da garganta. Brasileiro costuma fazer 'rr' de 'carro' ou omitir.",
    "ɪ": "I curto (ship, sit): som relaxado entre 'i' e 'ê'. Se falar 'i' longo vira 'sheep'.",
    "iː": "I longo (sheep, seat): 'i' bem esticado e tenso.",
    "i": "I longo (sheep, seat): 'i' bem esticado e tenso.",
    "æ": "A de 'cat, man': boca bem aberta, entre 'a' e 'é'. Não é o 'é' de 'pé'.",
    "ʌ": "U de 'cup, but': som curto parecido com 'â' de 'cama'. Não é 'u'.",
    "ə": "Schwa (about, teacher): vogal preguiçosa, quase muda. Não capriche na vogal.",
    "ɛ": "E de 'bed': 'é' curto.",
    "ʊ": "U curto de 'book, put': lábios relaxados.",
    "uː": "U longo de 'food': lábios bem arredondados.",
    "u": "U longo de 'food': lábios bem arredondados.",
    "ɑ": "A aberto de 'father, hot' (US): boca bem aberta.",
    "ɔ": "O de 'thought, law': boca arredondada.",
    "ŋ": "NG final (sing, going): som nasal no fundo da boca, sem soltar o 'g'.",
    "v": "V: dentes de cima no lábio de baixo. Não confunda com 'w'.",
    "w": "W: lábios arredondados como 'u' rápido. Não é 'v'.",
    "z": "Z sonoro (zoo, is): vibra a garganta. 'S' de 'is' e 'was' é Z, não S.",
    "s": "S surdo: só ar, sem voz.",
    "ʃ": "SH (she, ship): como o 'ch' de 'chave'.",
    "ʒ": "J de 'measure, vision': como o 'j' de 'já'.",
    "tʃ": "CH (chair, church): 'tch' como em 'tchau'.",
    "dʒ": "J de 'job, judge': 'dj' como em 'adjetivo'.",
    "l": "L: ponta da língua no céu da boca. No fim da palavra (feel, all) não vira 'u' como em português.",
    "t": "T: em inglês não vira 'tch' antes de 'i' (time não é 'tchaime').",
    "d": "D: não vira 'dj' antes de 'i' (did não é 'djid').",
    "p": "P inicial tem sopro de ar (aspiração): 'p-hen'.",
    "k": "K inicial tem sopro de ar (aspiração).",
    "eɪ": "Ditongo de 'day, make': 'êi' fechado.",
    "oʊ": "Ditongo de 'go, no': 'ôu', não 'ó' aberto.",
    "aɪ": "Ditongo de 'my, time': 'ai'.",
    "aʊ": "Ditongo de 'now, house': 'au'.",
    "ɜ": "ER de 'bird, work': vogal com R, língua enrolada. Não é 'ér'.",
    "ɝ": "ER de 'bird, work': vogal com R, língua enrolada. Não é 'ér'.",
}

ERROR_LABELS = {
    "Mispronunciation": "pronunciou errado",
    "Omission": "pulou a palavra",
    "Insertion": "acrescentou palavra que não existe na frase",
    "UnexpectedBreak": "pausou onde não devia",
    "MissingBreak": "não pausou onde devia",
    "Monotone": "falou sem entonação",
}


def summarize(assessment: dict) -> dict:
    """Recebe a saída de azure_speech.assess_pronunciation e devolve o resumo."""
    if not assessment.get("recognized") or not assessment.get("raw"):
        return {
            "recognized": False,
            "error": assessment.get("error") or "Nada reconhecido.",
            "scores": {},
            "words": [],
            "issues": [],
        }

    nbest = assessment["raw"]["NBest"][0]
    pa = nbest.get("PronunciationAssessment", {})
    scores = {
        "accuracy": round(pa.get("AccuracyScore", 0)),
        "fluency": round(pa.get("FluencyScore", 0)),
        "completeness": round(pa.get("CompletenessScore", 0)),
        "prosody": round(pa.get("ProsodyScore", 0)) if "ProsodyScore" in pa else None,
        "overall": round(pa.get("PronScore", 0)),
    }

    words_out = []
    issues = []
    for w in nbest.get("Words", []):
        wpa = w.get("PronunciationAssessment", {})
        score = round(wpa.get("AccuracyScore", 0))
        error = wpa.get("ErrorType", "None")
        bad_phonemes = []
        for ph in w.get("Phonemes", []):
            p_score = round(ph.get("PronunciationAssessment", {}).get("AccuracyScore", 100))
            if p_score < PHONEME_THRESHOLD:
                bad_phonemes.append({"phoneme": ph.get("Phoneme", "?"), "score": p_score})

        entry = {"word": w.get("Word", ""), "score": score, "error": error, "bad_phonemes": bad_phonemes}
        words_out.append(entry)

        if error != "None" or score < WORD_THRESHOLD:
            issues.append(entry)

    # Piores primeiro; omissões/inserções contam como nota 0 para ordenação
    issues.sort(key=lambda e: (0 if e["error"] in ("Omission", "Insertion") else e["score"]))
    issues = issues[:MAX_ISSUES]

    return {
        "recognized": True,
        "error": None,
        "transcript": assessment["text"],
        "scores": scores,
        "words": words_out,
        "issues": issues,
        "issues_text": describe_issues(issues),
    }


def describe_issues(issues: list[dict]) -> str:
    """Texto curto em português para o prompt do LLM."""
    if not issues:
        return "Nenhum erro relevante."
    lines = []
    for it in issues:
        label = ERROR_LABELS.get(it["error"], "")
        parts = [f"'{it['word']}' (nota {it['score']}"]
        if label:
            parts.append(f", {label}")
        parts.append(")")
        line = "".join(parts)
        if it["bad_phonemes"]:
            tips = []
            for bp in it["bad_phonemes"]:
                tip = PHONEME_TIPS.get(bp["phoneme"])
                tips.append(f"/{bp['phoneme']}/ nota {bp['score']}" + (f" — {tip}" if tip else ""))
            line += ": " + "; ".join(tips)
        lines.append("- " + line)
    return "\n".join(lines)
