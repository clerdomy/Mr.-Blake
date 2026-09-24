"""
Configurações do lean_ingles.
Tudo que é segredo vem de variável de ambiente (ver .env.example).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Carrega o .env da raiz do projeto. override=True: o que está no .env manda,
# mesmo que a variável já exista no terminal. Em produção não há .env, então as
# variáveis do Container App / Cloud Run continuam valendo.
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env", override=True)
except ImportError:
    pass

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")
CSRF_TRUSTED_ORIGINS = [
    o.strip().rstrip("/") for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]
# Azure Container Apps injeta o domínio do app nesta variável: confia nele automaticamente.
_aca_host = os.environ.get("CONTAINER_APP_HOSTNAME")
if _aca_host:
    CSRF_TRUSTED_ORIGINS.append(f"https://{_aca_host}")
# Atrás do ingress do Azure / Cloud Run o HTTPS termina no proxy e o Django recebe HTTP;
# este header diz ao Django que a conexão original era segura.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "teacher",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Sem banco de dados: o estado da conversa vive na sessão em cookie assinado.
# Isso permite rodar no Cloud Run sem Cloud SQL. O histórico é mantido curto
# (ver teacher/views.py) para caber no limite de 4 KB do cookie.
DATABASES = {}
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if not DEBUG
        else "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10 MB de áudio por request

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Manaus"
USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---- Integrações -------------------------------------------------------------
AZURE_SPEECH_KEY = os.environ.get("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION = os.environ.get("AZURE_SPEECH_REGION", "eastus")
AZURE_TTS_VOICE = os.environ.get("AZURE_TTS_VOICE", "pt-BR-Antonio:DragonHDOmniLatestNeural")

# Voz do professor: "azure" (com visemas) ou "openai" (voz mais natural, boca por volume)
TTS_PROVIDER = os.environ.get("TTS_PROVIDER", "azure")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_TTS_MODEL = os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
# Vozes: ash (masc., grave), ballad, coral, echo, fable, onyx (masc., profunda), nova, sage, shimmer, verse
OPENAI_TTS_VOICE = os.environ.get("OPENAI_TTS_VOICE", "ash")

# Qualquer API compatível com OpenAI: Groq, Together, OpenRouter, OpenAI...
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")

# Nível de "provocação" do professor: 1 (gentil) a 3 (sem dó)
TEACHER_SASS_LEVEL = int(os.environ.get("TEACHER_SASS_LEVEL", "2"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
