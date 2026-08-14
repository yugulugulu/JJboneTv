FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
ARG DEBIAN_MIRROR=https://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=https://deb.debian.org/debian-security
RUN sed -i \
        -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
EXPOSE 8000
CMD ["sh", "-c", "python -m app.runtime validate user-api && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
