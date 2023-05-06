# HTTPS API
This service is designed to support custom domains for SaaS products.

# Usage
First you need to set the proper environment variable for your desired installation.

Here is a sample `.env` file.
```
SAAS_UPSTREAM=example.com:443
API_KEY=0df05a6c-d4c6-4ee4-a55d-de1409e82cee
```

To make sure that certificates are saved, we create a volume for it.
```bash
docker volume create https_config
docker volume create https_data
docker volume create https_domains
docker volume create https_site
```

Now you can start our stack to run the compose file:
```bash
docker-compose up -d
```

This will run the webserver and the management APIs. 

The OpenAPI documentation for the management APIs is available at:
- https://localhost:9000/docs