# Conector externo DiagraMind — imagen única versionada (doc 26 §4).
#
# Todo el estado vive en /data (DMC_HOME): montarle un volumen persistente (en la
# nube: el PVC de Longhorn del tenant). El repo git queda en /data/repo.
#
#   docker build -t diagramind-connector .
#   docker run -p 8770:8770 -v conector-data:/data \
#     -e DMC_ADMIN_PASSWORD=... [-e DMC_ADMIN_MUST_CHANGE=0] \
#     [-e DMC_SHARED=1 -e DMC_FOLDER_QUOTA_MB=30] \        # instancia free (doc 26 §3)
#     diagramind-connector
#
# Sin DMC_ADMIN_PASSWORD el bootstrap es el interactivo de siempre (password
# aleatoria en consola y en /data/admin_password.txt).

FROM python:3.12-slim

# git: el root del conector ES un repo git (versionado/guardar, doc 25 §7)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY dashboard/ dashboard/

RUN useradd -m -u 1000 connector && mkdir -p /data && chown connector:connector /data
USER connector

ENV DMC_HOME=/data \
    DMC_HOST=0.0.0.0 \
    DMC_PORT=8770 \
    PYTHONUNBUFFERED=1

EXPOSE 8770
VOLUME /data

# Nota: k8s ignora este HEALTHCHECK (usa sus probes contra /health); sirve para
# docker suelto. Asume el puerto default — si cambiás DMC_PORT, ajustá la probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8770/health', timeout=4).status == 200 else 1)"]

CMD ["python", "server.py"]
