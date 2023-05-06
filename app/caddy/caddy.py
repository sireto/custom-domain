import os

from dotenv import load_dotenv
from fastapi import HTTPException
import validators

from app.caddy.caddy_config import CaddyAPIConfigurator

HTTPS_PORT = 443

DEFAULT_ADMIN_URL = 'http://localhost:2019'
DEFAULT_CADDY_FILE = "caddy.json"
DEFAULT_SAAS_UPSTREAM = "example.com:443"
DEFAULT_LOCAL_PORT = f"{HTTPS_PORT}"

load_dotenv()


class Caddy:

    def __init__(self):
        self.admin_url = os.environ.get('CADDY_ADMIN_URL', DEFAULT_ADMIN_URL)
        self.config_json_file = os.environ.get('CADDY_CONFIG_FILE', DEFAULT_CADDY_FILE)
        self.saas_upstream = os.environ.get('SAAS_UPSTREAM', DEFAULT_SAAS_UPSTREAM)
        self.local_port = os.environ.get('LOCAL_PORT', DEFAULT_LOCAL_PORT)
        self.disable_https = os.environ.get('DISABLE_HTTPS', 'False').upper() == "TRUE"

        self.configurator = CaddyAPIConfigurator(
            api_url=self.admin_url,
            https_port=self.local_port,
            disable_https=self.disable_https
        )

        if not self.configurator.load_config_from_file(self.config_json_file):
            self.configurator.init_config()

    def add_custom_domain(self, domain, upstream):
        if not validators.domain(domain):
            raise HTTPException(status_code=400, detail=f"{domain} is not a valid domain")

        upstream = upstream or self.saas_upstream
        if not self.configurator.add_domain(domain, upstream):
            raise HTTPException(status_code=400, detail=f"Failed to add domain: {domain}")

        self.configurator.save_config(self.config_json_file)

    def remove_custom_domain(self, domain):
        if not validators.domain(domain):
            raise HTTPException(status_code=400, detail=f"{domain} is not a valid domain")

        if not self.configurator.delete_domain(domain):
            raise HTTPException(status_code=400, detail=f"Failed to remove domain: {domain}. Might not be exist.")

        self.configurator.save_config(self.config_json_file)

    def deployed_config(self):
        return self.configurator.config

    def list_domains(self):
        return self.configurator.list_domains()


caddy_server = Caddy()
