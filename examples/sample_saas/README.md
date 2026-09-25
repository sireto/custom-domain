# Sample SaaS origin

A second, minimal application that proves the integration is reusable: it
selects its workspace from the edge assertion using the SDK middleware and
answers the workspace probe.

## Run it against the local Compose environment

1. Start the service with the edge enabled and local relaxations:

   ```
   ENABLE_LEGACY_API=false DISABLE_HTTPS=true ORIGIN_ALLOW_PRIVATE=true \
   EDGE_ASSERTION_KEYS="1:$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
   docker compose up
   ```

2. Register the application and its origin (this app, reachable from the
   container as `host.docker.internal:8000`; on Linux add
   `--add-host host.docker.internal:host-gateway` to the compose service):

   ```
   docker compose exec https custom-domain application create --slug sample --name "Sample SaaS" --cname-target sample.localtest.me
   docker compose exec https custom-domain origin register --application sample --host host.docker.internal --scheme http --port 8000
   ```

   Export the printed token as `ORIGIN_VERIFICATION_TOKEN`, the application id
   as `APPLICATION_ID`, the same `EDGE_ASSERTION_KEYS`, and start this app:

   ```
   uvicorn examples.sample_saas.app:app --port 8000
   docker compose exec https custom-domain origin verify --application sample --host host.docker.internal --activate
   ```

3. Issue a credential and register a workspace domain with the SDK:

   ```
   docker compose exec https custom-domain credential issue --application sample --label demo
   python - <<'EOF'
   from custom_domain import Client
   c = Client("http://127.0.0.1:9000", credential="cd_...")
   d = c.create_domain("alpha.sample.localtest.me", "ws_alpha", idempotency_key="demo-alpha")
   print(d.render_dns_instructions())
   EOF
   ```

   `*.localtest.me` resolves to 127.0.0.1, so the routing check needs the
   CNAME to be simulated: run `custom-domain checks dns` with a resolver of
   your own or mark the checks in the database for a local demo. With
   `DISABLE_HTTPS=true` the certificate check passes automatically; the
   workspace probe then reaches this app through Caddy and the domain
   becomes `ready`.

4. Open `http://alpha.sample.localtest.me` (through Caddy on port 80 in the
   local setup): the page names the Alpha workspace, and a second domain
   registered for `ws_beta` shows Beta on its own hostname.

Everything above uses invented hostnames and secrets; nothing here is a real
domain.
