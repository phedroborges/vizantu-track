FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN DEBUG=1 SECRET_KEY=build-only-key-never-used-at-runtime-000000000 python manage.py collectstatic --noinput \
    && useradd --create-home --uid 10001 app \
    && chown -R app:app /app
USER app
EXPOSE 8000
CMD ["bash", "boot.sh"]
