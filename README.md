<p align="center">
  <img src="img/app.svg" alt="Keycloak logo" width="80" height="80">
</p>

<h1 align="center">Keycloak Nextcloud ExApp</h1>

<p align="center">
  <strong>Keycloak identity and access management as a Nextcloud External Application -- shared OIDC provider for Common Ground ExApps</strong>
</p>

<p align="center">
  <a href="https://github.com/ConductionNL/keycloak-nextcloud/releases"><img src="https://img.shields.io/github/v/release/ConductionNL/keycloak-nextcloud" alt="Latest release"></a>
  <a href="https://github.com/ConductionNL/keycloak-nextcloud/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-EUPL--1.2-blue" alt="License"></a>
</p>

---

## Overview

This ExApp wraps [Keycloak](https://www.keycloak.org/) as a Nextcloud sidecar, providing a centralized OIDC identity provider for all Common Ground ExApps. It automatically syncs Nextcloud users to Keycloak and provides a token API for server-side authentication, enabling seamless SSO without browser-side OIDC redirects.

### Key Features

- **Automatic user sync** -- Nextcloud users are synced to Keycloak on startup, on-demand, and on user changes
- **Token API** -- Consumer ExApps (OpenTalk, OpenZaak, Valtimo) request Keycloak tokens server-side via a shared secret
- **Realm management** -- Auto-creates the `commonground` realm and configures OIDC clients
- **Direct access grant** -- Gets tokens for users without browser interaction
- **Admin console** -- Keycloak admin UI accessible from Nextcloud top menu

### Consumer ExApps

The Keycloak ExApp serves as the shared identity provider for:

| ExApp | Usage |
|-------|-------|
| [OpenTalk](https://github.com/ConductionNL/opentalk) | Video conferencing SSO -- iframe-embedded with pre-loaded tokens |
| [OpenZaak](https://github.com/ConductionNL/openzaak) | ZGW case management authentication |
| [Valtimo](https://github.com/ConductionNL/valtimo) | BPM and case management SSO |
| [OpenKlant](https://github.com/ConductionNL/openklant) | Customer interaction registry auth |

## Requirements

| Dependency | Version | Notes |
|-----------|---------|-------|
| Nextcloud | 30+ | |
| [AppAPI](https://apps.nextcloud.com/apps/app_api) | latest | Must be installed and configured with a deploy daemon |
| Docker | -- | Required for ExApp container deployment |
| PostgreSQL | 14+ | Keycloak database backend |
| Java 21 | -- | Bundled in the Docker image |

## Quick Start

The ExApp is included in the OpenRegister docker-compose setup:

```bash
docker compose -f openregister/docker-compose.yml --profile commonground up -d
```

Default Keycloak admin credentials: `admin` / `admin`

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `KEYCLOAK_REALM` | Realm name for user sync and token issuance | `commonground` |
| `KC_BOOTSTRAP_ADMIN_USERNAME` | Keycloak admin username | `admin` |
| `KC_BOOTSTRAP_ADMIN_PASSWORD` | Keycloak admin password | `admin` |
| `KC_HOSTNAME` | Hostname in issued tokens (must match consumer expectations) | -- |
| `KEYCLOAK_API_SECRET` | Shared secret for ExApp-to-ExApp token API calls | `keycloak-exapp-internal-secret` |

## Architecture

### Component Overview

The Keycloak ExApp container bundles two processes and connects to a shared PostgreSQL database:

| Component | Image / Technology | Role | Port |
|-----------|-------------------|------|------|
| **FastAPI Wrapper** | Python / nc_py_api | AppAPI lifecycle, user sync, token API, Keycloak proxy | 23002 (ExApp) |
| **Keycloak Server** | `quay.io/keycloak/keycloak:26.5` | OIDC identity provider, realm/client management, admin console | 8080 (internal), 8180 (host) |
| **PostgreSQL** | Shared with Nextcloud | Persistent storage for realms, users, clients, sessions | 5432 |

### Infrastructure Diagram

```mermaid
graph TB
    subgraph "Nextcloud Server"
        NC["Nextcloud + AppAPI"]
    end

    subgraph "Consumer ExApps"
        OT["OpenTalk ExApp<br/><i>Video conferencing</i>"]
        OZ["OpenZaak ExApp<br/><i>Case management</i>"]
        VL["Valtimo ExApp<br/><i>BPM platform</i>"]
        OK["OpenKlant ExApp<br/><i>Customer registry</i>"]
    end

    subgraph "Keycloak ExApp Container"
        FW["FastAPI Wrapper<br/><i>Port 23002</i><br/>User sync, token API,<br/>AppAPI lifecycle"]
        KS["Keycloak Server<br/><i>Port 8080</i><br/>OIDC provider,<br/>admin console"]
    end

    PG["PostgreSQL<br/><i>Port 5432</i><br/>Realms, users,<br/>clients, sessions"]

    NC -->|"AUTHORIZATION-APP-API<br/>User management events"| FW
    OT -->|"POST /api/token<br/>X-API-SECRET + X-NC-USER-ID"| FW
    OZ -->|"POST /api/token"| FW
    VL -->|"POST /api/token"| FW
    OK -->|"POST /api/token"| FW
    FW -->|"Admin REST API<br/>User CRUD, realm mgmt"| KS
    FW -->|"Direct access grant<br/>password → tokens"| KS
    KS -->|"Identity DB"| PG
    FW -->|"OCS API<br/>List/get NC users"| NC

    style FW fill:#c63,stroke:#333,color:#fff
    style KS fill:#e74,stroke:#333,color:#fff
    style PG fill:#36a,stroke:#333,color:#fff
    style OT fill:#369,stroke:#333,color:#fff
    style OZ fill:#369,stroke:#333,color:#fff
    style VL fill:#369,stroke:#333,color:#fff
    style OK fill:#369,stroke:#333,color:#fff
```

### Component Details

#### FastAPI Wrapper (`ex_app/lib/main.py`)

The Python wrapper manages the Keycloak process, syncs users, and exposes the token API for consumer ExApps.

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/heartbeat` | GET | None | AppAPI health check -- probes Keycloak management port |
| `/init` | POST | AppAPI | Starts Keycloak, creates realm, syncs all Nextcloud users |
| `/enabled` | PUT | AppAPI | Starts or stops the Keycloak process |
| `/api/token` | POST | Shared secret / AppAPI | Gets a Keycloak token for a Nextcloud user via direct access grant |
| `/api/sync-user` | POST | Shared secret / AppAPI | Syncs a single Nextcloud user to Keycloak |
| `/api/sync-all` | POST | Shared secret / AppAPI | Syncs all Nextcloud users to Keycloak |
| `/api/delete-user` | POST | Shared secret / AppAPI | Removes a user from Keycloak |
| `/*` | ALL | AppAPI | Proxied to Keycloak (admin console, well-known endpoints) |

#### Keycloak Server

[Keycloak](https://www.keycloak.org/) (v26.5) runs as a child process inside the container. It provides:

- **OIDC / OAuth2 provider** -- Issues JWT access tokens, refresh tokens, and ID tokens
- **Realm management** -- The `commonground` realm is auto-created on first start
- **Client registration** -- OIDC clients for each consumer ExApp (e.g., `opentalk`, `opentalk-controller`)
- **Direct access grant** -- Allows the wrapper to get tokens for users with known credentials (no browser needed)
- **Admin console** -- Full Keycloak admin UI accessible via the Nextcloud proxy
- **`KC_HOSTNAME`** -- Controls the `iss` claim in tokens; must match what consumers expect (e.g., `http://localhost:8180`)

#### PostgreSQL

Shared database with Nextcloud. Keycloak uses its own schema (`keycloak` database) for:
- Realm and client configuration
- User accounts (synced from Nextcloud)
- Active sessions and tokens
- Audit events

### Token API Flow

Consumer ExApps call the `/api/token` endpoint to get Keycloak tokens for Nextcloud users without any browser interaction:

```mermaid
sequenceDiagram
    participant C as Consumer ExApp<br/>(e.g. OpenTalk)
    participant FW as FastAPI Wrapper
    participant KS as Keycloak Server
    participant PW as Password Store<br/>(in-memory)

    C->>FW: POST /api/token<br/>X-API-SECRET: <secret><br/>X-NC-USER-ID: admin<br/>?client_id=opentalk

    FW->>PW: Lookup password for "admin"

    alt Password found
        FW->>KS: POST /realms/commonground/protocol/openid-connect/token<br/>grant_type=password&username=admin&password=<auto>
    else Password not found (first request)
        FW->>KS: Reset user password via Admin API
        KS-->>FW: OK
        FW->>PW: Store new password
        FW->>KS: POST /token (grant_type=password)
    end

    alt Token success
        KS-->>FW: {access_token, refresh_token, id_token, expires_in}
        FW-->>C: 200 {access_token, refresh_token, id_token, expires_in}
    else 401 (stale password)
        FW->>KS: Reset password + retry
        KS-->>FW: tokens
        FW-->>C: 200 tokens
    end
```

### User Sync

Users are synced from Nextcloud to Keycloak at three trigger points:

1. **On init** -- All Nextcloud users are synced when the ExApp starts
2. **On demand** -- When a token is requested for a user that doesn't exist in Keycloak yet
3. **Via API** -- `POST /api/sync-user` or `POST /api/sync-all` for manual sync

For each user, the sync process:
- Fetches user details from Nextcloud's OCS provisioning API
- Creates or updates the user in Keycloak's `commonground` realm
- Sets `firstName`, `lastName`, `email` from the Nextcloud profile
- Generates a random password and stores it in memory
- Enables the `direct access grant` on OIDC clients so tokens can be obtained server-side

### Authentication

The `/api/*` endpoints are excluded from Nextcloud's AppAPI middleware (`disable_for=["api/*"]`) and use two auth methods:

1. **Shared secret** (`X-API-SECRET` header) -- For direct ExApp-to-ExApp container calls. The secret is configured via the `KEYCLOAK_API_SECRET` environment variable and must match across all consumer ExApps.
2. **AppAPI auth** (`authorization-app-api` header) -- For requests proxied through Nextcloud. The user ID is decoded from the base64-encoded `userId:appSecret` value.

## Development

```bash
# Build Docker image
docker build -t ghcr.io/conductionnl/keycloak-nextcloud:latest .

# Copy changes to running container
docker cp ex_app/lib/main.py openregister-exapp-keycloak:/app/ex_app/lib/main.py
docker restart openregister-exapp-keycloak
```

## Links

| Resource | URL |
|----------|-----|
| Keycloak | [keycloak.org](https://www.keycloak.org/) |
| This ExApp (GitHub) | [ConductionNL/keycloak-nextcloud](https://github.com/ConductionNL/keycloak-nextcloud) |
| OpenTalk ExApp | [ConductionNL/opentalk](https://github.com/ConductionNL/opentalk) |
| Nextcloud AppAPI | [GitHub](https://github.com/nextcloud/app_api) / [Docs](https://docs.nextcloud.com/server/latest/developer_manual/exapp_development/) |

## License

EUPL-1.2 -- See [LICENSE](LICENSE) for details.

This license applies to the **Nextcloud ExApp wrapper only**. Keycloak is licensed under the [Apache License 2.0](https://github.com/keycloak/keycloak/blob/main/LICENSE.txt).

## Authors

Built by [Conduction B.V.](https://conduction.nl) -- open-source software for Dutch government and public sector organizations.
