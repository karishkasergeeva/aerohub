# Спільний Dockerfile мікросервісів AeroHub. Збирається з кореня репозиторію:
#   docker build -f services/Dockerfile --build-arg SERVICE=flight_service .
FROM python:3.12-slim
RUN useradd --create-home --uid 10001 aerohub
ARG SERVICE
WORKDIR /srv
COPY libs/aerohub_common /tmp/aerohub_common
COPY services/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock && pip install --no-cache-dir --no-deps /tmp/aerohub_common
COPY services/${SERVICE}/app ./app
ENV PYTHONUNBUFFERED=1
USER aerohub
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
