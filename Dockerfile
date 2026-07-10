# Voxniac ONE — imagen de contenedor para el AMD Developer Hackathon
# Base slim de Python 3.12 (misma version usada en desarrollo).
FROM python:3.12-slim

# No generar .pyc, salida sin buffer para que los logs aparezcan en tiempo real.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Instala dependencias primero para aprovechar la cache de capas de Docker:
# solo se reinstalan si requirements.txt cambia.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia el resto del codigo (respeta .dockerignore: no entran .git, .env,
# recordings/, transcripts/, leads.db, etc.)
COPY . .

# leads.db se crea vacio aqui (no viene en el build context, esta en
# .dockerignore) para que exista como ARCHIVO en la imagen: asi cuando
# docker-compose monta un volumen nombrado sobre /app/leads.db, Docker lo
# reconoce como mount de archivo (y no crea un directorio por error) y
# persiste el contenido entre reinicios del contenedor.

# Usuario no-root: reduce superficie de ataque dentro del contenedor.
# Le damos ownership de /app para que pueda escribir leads.db, recordings/,
# transcripts/ y logs en runtime (el volumen de leads.db se monta encima).
RUN useradd --create-home --uid 1000 voxniac \
    && mkdir -p /app/recordings /app/transcripts \
    && touch /app/leads.db \
    && chown -R voxniac:voxniac /app
USER voxniac

EXPOSE 8080

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
