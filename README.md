# Custom Domain API
This service is designed to support custom domains for SaaS products.

# Usage
Here are the steps you need to take to run this docker image.

## 1. Environment variables
Set the proper environment variable for your desired installation.

Here is a sample `.env` file.
```
SAAS_UPSTREAM=example.com:443
API_KEY=0df05a6c-d4c6-4ee4-a55d-de1409e82cee
DATABASE_URL=sqlite:///data/custom_domain.db
```
`DATABASE_URL` selects the authoritative database. Use a PostgreSQL URL such as
`postgresql+psycopg://user:password@db:5432/custom_domain` for production.

## 2. Create docker volumes to persist data (eg. certificates, domains, database)
```bash
docker volume create https_data
docker volume create https_domains
docker volume create https_db
```

## 3. Docker Compose file
Create a `docker-compose.yml` file.
```yaml
version: "3.7"

services:
  https:
    image: sireto/custom-domain:latest
    ports:
      - "80:80"
      - "443:443"
      - "443:443/udp"
      - "127.0.0.1:9000:9000"
    restart: unless-stopped
    networks:
      - frontend
    env_file:
      - .env
    volumes:
      - https_domains:/app/domains
      - https_data:/var/lib/custom-domain
      - https_db:/app/data

volumes:
  https_data:
    external: true
  https_domains:
    external: true
  https_db:
    external: true

networks:
  frontend:
```
The `https_data` volume holds Caddy's certificate store. Deployments created
before the mount path changed from `/root/.local/share` keep the same volume;
only the mount path in the compose file changes, and the container fixes the
file ownership on start.

Now you can run the compose file:
```bash
docker-compose up -d
```

This will run a webserver and the management APIs. 

The management API listens on port 9000, bound to `127.0.0.1` on the host so
it is not a public port; reach it from the host or through an authenticated
reverse proxy you control. Its OpenAPI document and Swagger UI are at
**http://localhost:9000/v1/docs**.

## 4. Instructions for SaaS customers
Your SaaS customers need to add a DNS Record to point their domain to your deployed server. This can be done in one of the two ways.
### 4.1 A Record
Assuming your deployed server has IP address: `XX.XX.XX.XX`. <br/>
Then your customers will have to set the DNS Record:
- Type: A Record 
- Name: customerdomain.com (or subdomain)
- IPv4 address: `XX.XX.XX.XX`

### 4.2 CNAME Record
Assuming your deployed server has a DNS name: `custom.example.com` <br/>
Then, your customer should set the following DNS records:
- Type: CNAME Record 
- Name: customerdomain.com (or subdomain)
- Target: `custom.example.com`

# Development
The project uses [uv](https://docs.astral.sh/uv/) for a reproducible environment.
```bash
uv sync                      # install dependencies from uv.lock
uv run pytest                # run the test suite on a temporary SQLite database
uv run custom-domain --help  # operator commands: migrations, applications, credentials, imports
```
Set `TEST_DATABASE_URL` to a PostgreSQL URL to run the same suite against PostgreSQL.

The application and domain data model, its invariants, the status model and
the migration path from volume-based deployments are described in
[docs/data-model.md](docs/data-model.md). The v1 API contract, with worked
examples, error codes, webhook payloads and the migration from the legacy
`/domains` endpoint, is in [docs/api-v1.md](docs/api-v1.md); the OpenAPI
document is [docs/openapi.json](docs/openapi.json) and is served at
`/v1/openapi.json` with Swagger UI at `/v1/docs`. DNS verification, its diagnostics and the status rules it drives are in
[docs/dns-verification.md](docs/dns-verification.md); on-demand certificates and the
HTTPS readiness probe are in [docs/tls-readiness.md](docs/tls-readiness.md); how requests are routed and
the signed workspace assertion origins verify are in [docs/edge-routing.md](docs/edge-routing.md); the status
machine, the workspace probe and the worker are in [docs/lifecycle.md](docs/lifecycle.md); webhook
subscriptions, signatures and delivery are in [docs/webhooks.md](docs/webhooks.md). The Python SDK
for integrating a SaaS application, with the assertion-verifying middleware, is in
[sdk/](sdk/README.md); a minimal second SaaS origin is in [examples/sample_saas/](examples/sample_saas/README.md). Certificate storage,
multi-instance coordination, backup and restore are covered by
[docs/decisions/0001-certificate-storage.md](docs/decisions/0001-certificate-storage.md)
and [docs/operations.md](docs/operations.md).

# Source Code
The full source code is available on GitHub <br/>
[**https://github.com/sireto/custom-domain**](https://github.com/sireto/custom-domain)


# Paid version and Support
Don't want to host it yourself? No problem! We do it for you. Here's what you get on the paid version:
- Unlimited domains
- A dedicated IP address
- 20TB of free traffic
- Webserver with 2GB RAM, 1vCPU
- Email support

**Price: $20 / month** <br/>
**Contact: info@sireto.com**