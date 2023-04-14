# HTTPS API
This service is designed to support custom domains for SaaS products.

# Usage
First set the main domain host where the custom domains should point to.
This is set on the environment config of `docker-compose.yml`.
```bazaar
CADDY_MAIN_DOMAIN_HOST: bettercollected.com
CADDY_MAIN_DOMAIN_PORT: 443
```

Once the configuration is ready, you can simply run the compose file:
```bash
docker-compose up -d
```

This will run the webserver and the management APIs. With the management APIs,
you can add custom domains. The OpenAPI documentation for the management APIs 
is available at:
https://localhost:9000/docs