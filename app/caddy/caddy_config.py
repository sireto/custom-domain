import logging
import time

import requests
import json

from app.caddy import saas_template
from app.caddy.saas_template import DomainAlreadyExists, DomainDoesNotExist


class CaddyAPIConfigurator:

    def __init__(self, api_url, https_port, disable_https=False):
        self.logger = logging.getLogger(__name__)
        self.api_url = api_url
        self.config = {}
        self.https_port = https_port
        self.disable_https = disable_https

    def init_config(self):
        config = saas_template.https_template(disable_https=self.disable_https)
        if self.load_new_config(config):
            self.config = config

    def load_new_config(self, config):
        try:
            # Update the Caddy configuration using the /load endpoint
            headers = {"Content-Type": "application/json"}
            response = requests.post(f"{self.api_url}/load", headers=headers, data=json.dumps(config))
            response.raise_for_status()

            self.logger.info(f"Configuration has been loaded from config:\n{config}")
            self.config = config
            return True
        except requests.exceptions.HTTPError as e:
            self.logger.error(f"An error occurred while loading the configuration: {e}")
            self.logger.error(f"Response content: {response.content.decode('utf-8')}")
            return False

    def load_config_from_file(self, file_path):
        try:
            with open(file_path, 'r') as config_file:
                config = json.load(config_file)
                success = self.load_new_config(config)
                if success:
                    self.config = config
                return success
        except FileNotFoundError as e:
            self.logger.error(f"An error occurred while loading the configuration: {e}")
            return False

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

    def add_domain(self, domain, upstream):
        try:
            config = self.config.copy()

            try:
                new_config = saas_template.add_https_domain(
                    domain,
                    upstream,
                    template=config,
                    port=self.https_port,
                    disable_https=self.disable_https
                )

                # Try loading new config. If not successful, load the previous config
                if self.load_new_config(new_config):
                    return True

                self.load_new_config(config)
                return False

            except DomainAlreadyExists as dae:
                self.logger.error(f"Domain '{domain} already exists somewhere else.")
                raise

        except requests.exceptions.HTTPError as e:
            self.logger.error(f"An error occurred while adding the domain '{domain}': {e}")
            raise

    def delete_domain(self, domain):
        try:
            config = self.config.copy()

            try:
                new_config = saas_template.delete_https_domain(domain, config, port=self.https_port)

                # Try loading new config. If not successful, load the previous config
                if self.load_new_config(new_config):
                    return True

                self.load_new_config(config)
                return False

            except DomainDoesNotExist:
                self.logger.error(f"Domain '{domain} does not exist.")
                raise

        except requests.exceptions.HTTPError as e:
            self.logger.error(f"An error occurred while deleting the domain '{domain}': {e}")
            # self.logger.error(f"Response content: {response.content.decode('utf-8')}")
            raise

    def list_domains(self):
        try:
            # Fetch the entire Caddy configuration
            response = requests.get(f"{self.api_url}/config/")
            response.raise_for_status()
            config = response.json()

            # Add domain to match
            domains = saas_template.list_domains(config, port=self.https_port)
            return domains
        except requests.exceptions.HTTPError as e:
            self.logger.error(f"An error occurred while listing domains: {e}")
            self.logger.error(f"Response content: {response.content.decode('utf-8')}")
            return


if __name__ == "__main__":
    CADDY_API_URL = "http://localhost:2019"
    SERVER_PORT = 443
    CADDY_FILE = "caddy.json"

    SAAS_UPSTREAM = "example.com:443"  # this is where you SaaS should be available.
    DEV_UPSTREAM = "example.com:443"

    configurator = CaddyAPIConfigurator(CADDY_API_URL, SERVER_PORT, disable_https=False)
    if not configurator.load_config_from_file(CADDY_FILE):
        configurator.init_config()

    # Assuming these are customer domains you want to support
    custom_domains = [
        "customer1.domain.localhost",
        "customer2.domain.localhost",
        "customer3.domain.localhost",
        "customer4.domain.localhost",
        "customer5.domain.localhost",
    ]

    for custom_domain in custom_domains:
        configurator.add_domain(custom_domain, SAAS_UPSTREAM)

    # We want to save the config so the changes persist during restart
    configurator.save_config(CADDY_FILE)

    # To check if the custom domains are setup, checkout
    # https://customer1.domain.localhost
    # https://customer2.domain.localhost
    # and so on.
    #
    # Note that, we're using *.localhost for this local test. It is
    # because Caddy creates certificates for *.localhost domains.
    # If it is not working for you, then you might need to run
    # $ caddy trust
    #
    # This will install Caddy CA to your host operating system
    # and the HTTPS certificate will be trusted.
