FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-calc libreoffice-impress poppler-utils fonts-dejavu fonts-liberation fonts-crosextra-carlito \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY scripts scripts
COPY deploy/start.sh /start.sh
ENV PYTHONPATH=/app JOB_MEMORY_MB=1800
EXPOSE 8000
CMD ["/start.sh"]
