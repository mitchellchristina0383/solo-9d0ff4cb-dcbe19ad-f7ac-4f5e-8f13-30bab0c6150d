FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PITR_DATA_DIR=/data \
    PITR_PACKAGES_DIR=/packages \
    PITR_PACKAGE_TTL=86400 \
    PORT=8000

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pitr ./pitr
COPY tools ./tools
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh \
 && addgroup --system --gid 10001 pitr \
 && adduser --system --uid 10001 --ingroup pitr --home /app pitr \
 && mkdir -p /data /packages \
 && chown -R pitr:pitr /app /data /packages

# Bake the deterministic sample archive into the image so `docker compose up`
# works from a clean checkout with no host-side preparation.  An empty mounted
# /data makes the entrypoint regenerate it instead.
RUN python /app/tools/make_sample.py --out /data \
 && chown -R pitr:pitr /data

USER pitr

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", \
     "--access-logfile", "-", "--error-logfile", "-", "pitr.webapp:app"]
