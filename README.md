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
```

## 2. Create docker volumes to persist data (eg. certificates, domains etc.)
```bash
docker volume create https_config
docker volume create https_data
docker volume create https_domains
docker volume create https_site
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
      - "9000:9000"
    restart: unless-stopped
    networks:
      - frontend
    env_file:
      - .env
    volumes:
      - https_domains:/app/domains
      - https_site:/srv
      - https_data:/data
      - https_config:/config

volumes:
  https_data:
    external: true
  https_config:
    external: true
  https_site:
    external: true
  https_domains:
    external: true

networks:
  frontend:
```
Now you can run the compose file:
```bash
docker-compose up -d
```

This will run a webserver and the management APIs. 

The OpenAPI docs for the management APIs will be available on port 9000. <br/>
So, if you deployed host is localhost, the APIs are available at: <br/>
**http://localhost:9000/docs**

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