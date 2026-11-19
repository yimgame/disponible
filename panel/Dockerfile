# Imagen solo con dependencias: el código se monta (ro) desde el NAS en /srv/app.
FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi==0.115.* "uvicorn[standard]==0.32.*" jinja2==3.1.* argon2-cffi==23.1.* \
        httpx==0.27.* python-multipart==0.0.* tzdata \
    && useradd -u 1000 -m panel && mkdir /data && chown panel:panel /data
USER panel
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
