FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 THESISTRADE_ROLE=cloud THESISTRADE_DATA_DIR=/data
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir pypdf==6.10.0 cryptography==46.0.5
COPY ashare ./ashare
COPY config.cloud.json ./config.cloud.json
COPY docs ./docs
CMD ["python", "-m", "ashare.cli", "--config", "/app/config.cloud.json", "serve"]
