# Keycloak ExApp for Nextcloud
# Wraps Keycloak identity management with AppAPI integration
#
# Build: docker build -t ghcr.io/conductionnl/keycloak-nextcloud:latest .

# Stage 1: Get Keycloak distribution from official image
FROM quay.io/keycloak/keycloak:26.5.4 AS keycloak

# Stage 2: Runtime with Python + Java + Keycloak
FROM registry.access.redhat.com/ubi9/ubi-minimal:latest

# Install Java (same JDK 21 that Keycloak expects) and Python
RUN microdnf install -y \
        java-21-openjdk-headless \
        python3.11 \
        python3.11-pip \
    && microdnf clean all \
    && ln -sf /usr/bin/python3.11 /usr/bin/python3

# Copy Keycloak from the official image
COPY --from=keycloak /opt/keycloak /opt/keycloak

# Install Python packages
WORKDIR /app
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY ex_app/ ex_app/
COPY img/ ex_app/img/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Persistent data directory
VOLUME /data

# Keycloak environment
ENV KC_HOME="/opt/keycloak"
ENV PATH="/opt/keycloak/bin:${PATH}"

ENTRYPOINT ["./entrypoint.sh"]
