import os

from dotenv import load_dotenv
from fastapi import HTTPException
import validators

from app.caddy.caddy_config import CaddyAPIConfigurator

HTTPS_PORT = 443

DEFAULT_ADMIN_URL = 'http://localhost:2019'
DEFAULT_CADDY_FILE = "config.json"
DEFAULT_SAAS_HOST = "caddyserver.com"
DEFAULT_SAAS_PORT = f"{HTTPS_PORT}"
DEFAULT_CADDY_EMAIL = "info@example.com"

load_dotenv()


class Caddy:

    def __init__(self):
        self.admin_url = os.environ.get('CADDY_ADMIN_URL', DEFAULT_ADMIN_URL)
        self.email = os.environ.get('CADDY_EMAIL', DEFAULT_CADDY_EMAIL)
        self.config_json_file = os.environ.get('CADDY_CONFIG_FILE', DEFAULT_CADDY_FILE)
        self.saas_host = os.environ.get('SAAS_HOST', DEFAULT_SAAS_HOST)
        self.saas_port = os.environ.get('SAAS_PORT', DEFAULT_SAAS_PORT)

        self.configurator = CaddyAPIConfigurator(self.admin_url, self.email, self.saas_host, self.saas_port)
        if not self.configurator.load_config_file(self.config_json_file):
            self.configurator.init_config()

    def add_custom_domain(self, domain):
        if not validators.domain(domain):
            raise HTTPException(status_code=400, detail=f"{domain} is not a valid domain")

        if not self.configurator.add_domain(domain):
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
