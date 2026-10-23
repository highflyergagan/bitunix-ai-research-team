FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY tests ./tests
RUN useradd --uid 10001 --create-home desk && mkdir -p /app/data && chown desk:desk /app/data
USER desk
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s --retries=3 CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=4)"
# Exactly ONE worker: the scanner runs in-process and must not be duplicated.
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8765","--workers","1"]
