"""
Interpretador da fala do professor.

O LLM devolve a fala já dividida em trechos, cada um com o idioma decidido por ele:
    [{"lang": "pt", "text": "Não é"},
     {"lang": "en", "text": "fink"},
     {"lang": "pt", "text": ", é"},
     {"lang": "en", "text": "think"},
     {"lang": "sound", "ipa": "θ", "text": "th"}]

    pt     português do Brasil
    en     inglês americano
    sound  som isolado: lido com pausa antes e depois ("ipa" = símbolo IPA, "text" = em letras)

Este módulo valida os trechos e corrige instruções conhecidas em português que
vieram marcadas como inglês. Não tenta detectar o idioma de texto arbitrário.
Quem transforma em áudio é o tts.py / azure_speech.py.
"""
import re

PT, EN, SOUND = "pt", "en", "sound"
LOCALE = {PT: "pt-BR", EN: "en-US", SOUND: "en-US"}

# Instruções do professor às vezes vêm junto do exemplo, no mesmo trecho "en".
# Só corrige comandos inequívocos no início; exemplos livres continuam com o
# idioma escolhido pelo LLM. O restante do trecho mantém o inglês.
_PT_INSTRUCTION = re.compile(
    r"^(?:(?:agora\s+)?(?:"
    r"(?:tenta|tente)\s+(?:isso|essa(?:\s+frase)?|esta\s+frase|de\s+novo)|"
    r"(?:repete|repita)\s+(?:comigo|isso|essa(?:\s+frase)?|esta\s+frase|a\s+frase(?:\s+inteira)?)|"
    r"(?:fala|fale|diz|diga)\s+(?:isso|essa\s+frase|só\s+(?:a\s+)?palavra)"
    r")|agora\s+só\s+(?:a\s+)?palavra)"
    r"(?=\s|[:,.!?;—–-]|$)[\s:,.!?;—–-]*",
    re.IGNORECASE,
)


def normalize(raw) -> list[dict]:
    """Aceita a lista do LLM (ou uma string solta, tratada como português) e devolve trechos válidos."""
    if isinstance(raw, str):
        raw = [{"lang": PT, "text": raw}]
    segs = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        lang = str(item.get("lang", "")).lower()
        text = str(item.get("text") or "").strip()
        ipa = str(item.get("ipa") or "").strip().strip("/[]")
        if lang == SOUND and ipa:
            segs.append({"lang": SOUND, "ipa": ipa, "text": text or ipa})
        elif lang in (PT, EN, SOUND) and text:
            # "sound" sem IPA vira inglês comum, para não perder o trecho
            lang = EN if lang == SOUND else lang
            instruction = _PT_INSTRUCTION.match(text) if lang == EN else None
            if instruction:
                segs.append({"lang": PT, "text": instruction.group().strip()})
                text = text[instruction.end():].strip()
            if text:
                segs.append({"lang": lang, "text": text})
    return segs


def display_text(segs: list[dict]) -> str:
    """Texto para a tela: junta os trechos; som isolado aparece como /θ/."""
    out = ""
    for s in segs:
        piece = f"/{s['ipa']}/" if s["lang"] == SOUND else s["text"]
        if out and not re.match(r"[,.;:!?)]", piece):
            out += " "
        out += piece
    return out


def english_parts(segs: list[dict]) -> list[str]:
    return [s["text"] for s in segs if s["lang"] == EN]
