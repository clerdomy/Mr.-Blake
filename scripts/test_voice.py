"""
Ouça várias vozes do Azure com a mesma frase, sem abrir o app.

    python scripts/test_voice.py
    python scripts/test_voice.py "en-US-Andrew:DragonHDOmniLatestNeural" "pt-BR-Antonio:DragonHDOmniLatestNeural"

Gera um mp3 por voz em scripts/vozes/ e mostra se alguma voz deu erro
(voz inexistente na região, preview não liberado, etc.).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from teacher.services import azure_speech  # noqa: E402

FRASE = ("Fink? Não, meu amigo. É [think], com a língua entre os dentes. "
         "Repete comigo: [I think the weather is really nice today].")

PADRAO = [
    "en-US-Andrew:DragonHDOmniLatestNeural",
    "pt-BR-Antonio:DragonHDOmniLatestNeural",
    "pt-BR-Francisca:DragonHDOmniLatestNeural",
    "en-US-Andrew2:DragonHDLatestNeural",
    "en-US-AndrewMultilingualNeural",
    "pt-BR-MacerioMultilingualNeural",
]


def main():
    vozes = sys.argv[1:] or PADRAO
    out = Path(__file__).resolve().parent / "vozes"
    out.mkdir(exist_ok=True)
    for voz in vozes:
        try:
            r = azure_speech.synthesize_with_visemes(FRASE, voice=voz)
            dest = out / (voz.replace(":", "_") + ".mp3")
            dest.write_bytes(r["audio_mp3"])
            print(f"OK   {voz:45s} -> {dest.name}  ({len(r['visemes'])} visemas)")
        except Exception as e:
            print(f"ERRO {voz:45s} -> {str(e)[:120]}")
    print(f"\nAbra a pasta {out} e ouça os mp3.")


if __name__ == "__main__":
    main()
