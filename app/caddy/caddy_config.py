import logging

import requests
import json

from app.caddy import saas_template


class CaddyAPIConfigurator:

    def __init__(self, api_url, email, saas_host, saas_port=443):
        self.logger = logging.getLogger(__name__)
        self.api_url = api_url
        self.email = email
        self.upstream = f"{saas_host}:{saas_port}"
        self.config = {}

    def save_config(self, file_path):
        try:
            # Fetch the entire Caddy configuration
            response = requests.get(f"{self.api_url}/config/")
            response.raise_for_status()
            config = response.json()

            # Save the configuration to a file
            with open(file_path, 'w') as config_file:
                json.dump(config, config_file, indent=2)

            self.logger.info(f"Configuration has been saved to {file_path}.")

        except requests.exceptions.HTTPError as e:
            self.logger.error(f"An error occurred while saving the configuration: {e}")
            self.logger.error(f"Response content: {response.content.decode('utf-8')}")
            return

    def init_config(self):
        config = saas_template.https_template(self.upstream)
        if self.load_config(config):
            self.config = config

    def load_config(self, config):
        try:
            # Update the Caddy configuration using the /load endpoint
            headers = {"Content-Type": "application/json"}
            response = requests.post(f"{self.api_url}/load", headers=headers, data=json.dumps(config))
            response.raise_for_status()

            self.logger.info(f"Configuration has been loaded from config:\n{config}")
            return True
        except requests.exceptions.HTTPError as e:
            self.logger.error(f"An error occurred while loading the configuration: {e}")
            self.logger.error(f"Response content: {response.content.decode('utf-8')}")
            return False

    def load_config_file(self, file_path):
        try:
            with open(file_path, 'r') as config_file:
                config = json.load(config_file)
                success = self.load_config(config)
                if success:
                    self.config = config
                return success
        except FileNotFoundError as e:
            self.logger.error(f"An error occurred while loading the configuration: {e}")
            return False

    def add_domain(self, domain, allow_duplicates=True):
        try:
            # Check if there is a http route set for saas internal address
            response = requests.get(f"{self.api_url}/id/{self.upstream}")
            response.raise_for_status()

            # Fetch the entire Caddy configuration
            response = requests.get(f"{self.api_url}/config/")
            response.raise_for_status()
            config = response.json()

            # Add domain to match
            new_config = saas_template.add_domains(config, [domain])
            if new_config == config:
                if allow_duplicates:
                    return True

                if self.load_config(new_config):
                    self.config = new_config
                    self.logger.info(f"Domain '{domain}' has been added.")
                    return True
            return False
        except requests.exceptions.HTTPError as e:
            self.logger.error(f"An error occurred while adding the domain '{domain}': {e}")
            self.logger.error(f"Response content: {response.content.decode('utf-8')}")
            raise

    def delete_domain(self, domain):
        try:
            # Check if there is a http route set for saas internal address
            response = requests.get(f"{self.api_url}/id/{self.upstream}")
            response.raise_for_status()

            # Fetch the entire Caddy configuration
            response = requests.get(f"{self.api_url}/config/")
            response.raise_for_status()
            config = response.json()

            # Add domain to match
            new_config = saas_template.delete_domains(config, [domain])
            if new_config != config:
                if self.load_config(new_config):
                    self.config = new_config
                    self.logger.info(f"Domain '{domain}' has been deleted.")
                    return True
            return False
        except requests.exceptions.HTTPError as e:
            self.logger.error(f"An error occurred while deleting the domain '{domain}': {e}")
            self.logger.error(f"Response content: {response.content.decode('utf-8')}")
            raise

    def list_domains(self):
        try:
            # Check if there is a http route set for saas internal address
            response = requests.get(f"{self.api_url}/id/{self.upstream}")
            response.raise_for_status()

            # Fetch the entire Caddy configuration
            response = requests.get(f"{self.api_url}/config/")
            response.raise_for_status()
            config = response.json()

            # Add domain to match
            domains = saas_template.list_domains(config)
            return domains
        except requests.exceptions.HTTPError as e:
            self.logger.error(f"An error occurred while listing domains: {e}")
            self.logger.error(f"Response content: {response.content.decode('utf-8')}")
            return


if __name__ == "__main__":
    CADDY_API_URL = "http://localhost:2019"
    CADDY_FILE = "caddy.config.json"
    CADDY_EMAIL = "info@example.com"
    SAAS_HOST = "example.com"

    configurator = CaddyAPIConfigurator(CADDY_API_URL, CADDY_EMAIL, SAAS_HOST)
    if not configurator.load_config_file(CADDY_FILE):
        configurator.init_config()

    configurator.add_domain("localtest.me")
    configurator.add_domain("c1.localhost.me")
    configurator.add_domain("c2.localhost.me")
    configurator.add_domain("c3.localhost.me")

    configurator.save_config(CADDY_FILE)
