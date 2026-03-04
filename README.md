# Keycloak Nextcloud ExApp

Keycloak identity and access management as a Nextcloud External Application (ExApp).

## Overview

This ExApp wraps [Keycloak](https://www.keycloak.org/) as a Nextcloud sidecar, providing:

- Single Sign-On (SSO) via OpenID Connect
- User federation and identity brokering
- Fine-grained authorization
- Admin console accessible from Nextcloud

Serves as the shared OIDC identity provider for Common Ground ExApps (OpenZaak, OpenKlant, OpenTalk, Valtimo).

## Requirements

- Nextcloud 30+
- AppAPI app installed and configured
- PostgreSQL database

## Quick Start

The ExApp is included in the OpenRegister docker-compose setup:

```bash
docker compose -f openregister/docker-compose.yml --profile commonground up -d
```

Default admin credentials: `admin` / `admin`

## Development

```bash
# Build Docker image
make build

# Run locally
make run

# Code quality
make check-strict
```

## License

EUPL-1.2
