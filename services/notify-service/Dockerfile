FROM python@sha256:ea737b7f47756db87a12d3e0557c1c830fb0afd2c838a0edce44cfc675aa6513 AS builder
WORKDIR /build
COPY requirements.lock .
RUN pip install --no-cache-dir --no-compile --only-binary=:all: --default-timeout=100 --retries=3 --target /runtime-packages -r requirements.lock
RUN rm -rf /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/ensurepip /usr/local/include /usr/local/lib/pkgconfig /usr/local/share && mv /runtime-packages /usr/local/lib/python3.11/site-packages && rm -rf /usr/local/lib/python3.11/site-packages/bin
FROM scratch
COPY --from=builder /usr/local/ /usr/local/
COPY --from=builder /lib/ /lib/
COPY --from=builder /usr/lib/ /usr/lib/
COPY --from=builder /etc/ /etc/
COPY --from=builder /tmp/ /tmp/
WORKDIR /app
COPY --chown=10001:10001 app/ /app/app/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/usr/local/bin:/usr/bin:/bin
USER 10001:10001
EXPOSE 8001
CMD ["/usr/local/bin/python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
