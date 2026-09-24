FROM python:3.12-slim

# ffmpeg: fallback para converter áudio que não vier em WAV 16k (ex.: Safari antigo)
# libssl/ca-certificates/libasound2: exigidos pelo SDK do Azure Speech
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates libssl3 libasound2 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN DJANGO_SECRET_KEY=build python manage.py collectstatic --noinput

CMD exec gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120
