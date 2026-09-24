# lean_ingles — Mr. Blake, o professor de pronúncia que zoa você

Web app mobile-first em Django. O aluno segura o botão, fala a frase, o Azure
avalia a pronúncia fonema a fonema, um LLM (Llama via Groq por padrão) responde
com a personalidade do professor, e um boneco fala a resposta com a boca
sincronizada pelos visemas do Azure TTS.

```
aluno fala ──▶ navegador grava WAV 16k ──▶ POST /api/assess
                                              │
                    Azure Pronunciation Assessment (notas + fonemas)
                                              │
                          summarize.py (resumo compacto + dicas p/ brasileiros)
                                              │
                        LLM (Groq/Llama, JSON: reply, next_sentence, mood)
                                              │
                    Azure TTS (mp3 + visemas) ──▶ boneco fala + próxima frase
```

## Rodar local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # preencha AZURE_SPEECH_KEY, AZURE_SPEECH_REGION, LLM_API_KEY
export $(grep -v '^#' .env | xargs)
python manage.py runserver 0.0.0.0:8000
```

Abra no celular pelo IP da máquina. **Atenção:** o microfone só funciona em
`https://` ou em `localhost`. Para testar no celular, use um túnel
(`ngrok http 8000` ou `cloudflared tunnel --url http://localhost:8000`) e
adicione a URL em `DJANGO_CSRF_TRUSTED_ORIGINS`.

### Testar sem navegador

```bash
python scripts/test_assess.py meu_audio.wav "I think the weather is really nice today." --tts
```

Imprime a transcrição, salva o JSON bruto do Azure em `last_raw.json`, mostra o
resumo e a resposta do professor. Comece por aqui para calibrar os limiares em
`teacher/services/summarize.py` (`PHONEME_THRESHOLD`, `WORD_THRESHOLD`).

## Deploy no Azure Container Apps (mesma conta do Speech, franquia gratuita)

```powershell
az login
az extension add --name containerapp --upgrade
az provider register -n Microsoft.App --wait
az group create -n lean-ingles -l eastus
az containerapp up -n lean-ingles -g lean-ingles -l eastus --source . --ingress external --target-port 8080 `
  --env-vars "DJANGO_DEBUG=0" "DJANGO_SECRET_KEY=<gerada>" "AZURE_SPEECH_KEY=<chave>" "AZURE_SPEECH_REGION=eastus" `
  "AZURE_TTS_VOICE=en-US-Andrew:DragonHDOmniLatestNeural" "LLM_API_KEY=<chave groq>" "LLM_MODEL=openai/gpt-oss-120b"
# depois, com a URL impressa:
az containerapp update -n lean-ingles -g lean-ingles --set-env-vars "DJANGO_CSRF_TRUSTED_ORIGINS=https://<url>" --cpu 0.5 --memory 1Gi --min-replicas 0
```

Escala a zero; franquia gratuita de 180k vCPU-s, 360k GiB-s e 2M requisições/mês.
Se o CLI criar um Azure Container Registry, ele custa ~US$5/mês; alternativa é publicar
a imagem no GitHub Container Registry (grátis) e usar `--image ghcr.io/<user>/lean-ingles`.

Alternativa no Google: `gcloud run deploy lean-ingles --source . --region us-east1 --allow-unauthenticated --memory 1Gi`
com as mesmas variáveis em `--set-env-vars`.

Não há banco: a conversa vive num cookie de sessão assinado (últimas 6 trocas).

## O boneco (Rive)

Sem arquivo `.riv`, o app usa o rosto SVG em `templates/teacher/index.html`
com as bocas definidas em `static/teacher/avatar.js`. Para trocar por um
personagem feito no [Rive](https://rive.app):

1. Crie o personagem com uma state machine chamada **`Teacher`**.
2. Inputs: `viseme` (Number, 0–21, tabela de visemas do Azure), `mood`
   (Number: 0 neutral, 1 happy, 2 sassy, 3 shocked), `talking` (Boolean).
3. Exporte como `teacher/static/teacher/teacher.riv`. O app detecta e carrega
   sozinho.

Mapeamento visema → boca (referência para desenhar):

| visema | sons | boca |
|---|---|---|
| 0, 21 | silêncio, p b m | fechada |
| 1, 4, 5 | æ ə ʌ ɛ ʊ ɝ | meio aberta |
| 2, 9, 11 | ɑ aʊ aɪ | bem aberta |
| 3, 8, 10 | ɔ o ɔɪ | arredondada |
| 6 | j i ɪ | sorriso estreito |
| 7, 13, 16 | w u ɹ ʃ tʃ dʒ | biquinho |
| 12, 14, 19, 20 | h l d t n θ k g ŋ | levemente aberta |
| 15 | s z | dentes |
| 17 | ð | língua entre os dentes |
| 18 | f v | dente no lábio |

## Ajustes rápidos

- Personalidade: `TEACHER_SASS_LEVEL` (1–3) e `SYSTEM_PROMPT` em `teacher/services/llm.py`.
- Voz: `AZURE_TTS_VOICE` (Davis, Guy, Jenny, Aria, Ryan…). Estilos (cheerful, excited…) só em vozes que suportam.
- Provedor de LLM: `LLM_BASE_URL` + `LLM_MODEL` (qualquer API compatível com OpenAI).
- Dicas de fonema para brasileiros: `PHONEME_TIPS` em `summarize.py`.
- Tempo máximo de gravação: `maxSeconds` em `app.js` (15 s).

## Custos (ordem de grandeza)

Azure Speech tem 5 h/mês grátis (STT+assessment) e 0,5 M caracteres/mês de TTS neural.
Groq tem tier gratuito com rate limit. Um turno típico consome ~5 s de áudio
avaliado, ~200 caracteres de TTS e ~800 tokens de LLM.
