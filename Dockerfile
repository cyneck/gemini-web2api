FROM python:3.12-slim

# OCI metadata
LABEL org.opencontainers.image.title="gemini-web2api" \
      org.opencontainers.image.description="Gemini Web to OpenAI/Claude/Google compatible API proxy" \
      org.opencontainers.image.source="https://github.com/cyneck/gemini-web2api" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY gemini_web2api/ ./gemini_web2api/
COPY gemini_web2api.py ./

# Deliberately no baked-in config.json: the previous image shipped
# config.example.json as /app/config.json, which meant every container started
# with the demo key "sk-gemini" and a 0.0.0.0 listener. Mount a real config and
# cookie file, or pass settings through environment-driven flags.
#   docker run -v $PWD/config.json:/config/config.json \
#              -v $PWD/cookie.txt:/config/cookie.txt \
#              -p 127.0.0.1:8081:8081 gemini-web2api
VOLUME ["/config"]

# Run unprivileged.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /config \
    && chown -R appuser:appuser /app /config
USER appuser

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["python", "-m", "gemini_web2api"]
CMD ["--config", "/config/config.json", "--host", "0.0.0.0"]
