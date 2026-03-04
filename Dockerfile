FROM python:3.11-slim AS python-builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/python-packages -r requirements.txt


FROM quay.io/keycloak/keycloak:26.5.4

USER root

# Install Python runtime (UBI9-based image)
RUN microdnf install -y python3.11 python3.11-pip && microdnf clean all

# Copy pre-built Python packages from builder
COPY --from=python-builder /python-packages/lib/python3.11/site-packages/ /usr/lib/python3.11/site-packages/
COPY --from=python-builder /python-packages/bin/ /usr/local/bin/

# Create app directory
WORKDIR /app

# Copy application code
COPY ex_app/ ex_app/
COPY img/ ex_app/img/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Persistent data directory
VOLUME /data

ENTRYPOINT ["./entrypoint.sh"]
