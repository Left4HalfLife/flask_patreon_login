FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FLASK_APP=patron_blog:create_app \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 studio \
    && useradd --uid 10001 --gid studio --no-create-home studio \
    && mkdir -p /data/uploads \
    && chown -R studio:studio /data \
    && chmod 700 /data /data/uploads
COPY --chown=studio:studio patron_blog ./patron_blog
USER studio
EXPOSE 8000
CMD ["sh", "-c", "flask init-db && exec gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 120 --worker-tmp-dir /tmp 'patron_blog:create_app()'"]
