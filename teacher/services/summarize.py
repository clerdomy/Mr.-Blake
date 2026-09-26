"""
Transforma o JSON bruto do Pronunciation Assessment em um resumo compacto,
pensado para (1) o LLM saber exatamente o que errou e (2) o front colorir as palavras.
A explicação de cada som quem faz é o LLM, a partir dos fonemas em IPA.

Estrutura do JSON bruto (resumida):
  raw["NBest"][0]["PronunciationAssessment"] -> AccuracyScore, FluencyScore,
      CompletenessScore, PronScore, ProsodyScore
  raw["NBest"][0]["Words"][i] -> Word, PronunciationAssessment{AccuracyScore, ErrorType},
      Phonemes[j] -> Phoneme (IPA), PronunciationAssessment{AccuracyScore}
"""

PHONEME_THRESHOLD = 60   # fonema abaixo disso é "problema"
WORD_THRESHOLD = 70      # palavra abaixo disso entra na lista de erros
MAX_ISSUES = 4           # quantas palavras problemáticas mandar ao LLM


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

    # Piores primeiro; omissão conta como 0. Inserção vai pro fim, igual ao coach
    # (que ignora inserções), para o LLM não falar de uma palavra e o coach cobrar outra.
    issues.sort(key=lambda e: 0 if e["error"] == "Omission" else (101 if e["error"] == "Insertion" else e["score"]))

    return {
        "recognized": True,
        "error": None,
        "transcript": assessment["text"],
        "scores": scores,
        "words": words_out,
        "issues": issues[:MAX_ISSUES],
    }
