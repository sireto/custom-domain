import json
import os

import httpx
from dotenv import load_dotenv
from fastapi import HTTPException

from app.caddy.reverse_proxy import reverse_proxy_config

HTTPS_PORT = 443

load_dotenv()

class Caddy:

    def __init__(self):
        self.admin_url = os.environ.get('CADDY_ADMIN_URL', 'http://localhost:2019')
        self.config_json_file = os.environ.get('CADDY_CONFIG_JSON_FILE', 'domains/caddy.json')
        self.main_domain_host = os.environ.get('CADDY_MAIN_DOMAIN_HOST', 'caddyserver.com')
        self.main_domain_port = os.environ.get('CADDY_MAIN_DOMAIN_PORT', '443')

        self.config = Caddy.default_config()
        self.load_config_file()

    def load_config_file(self):
        if os.path.exists(self.config_json_file):
            with open(self.config_json_file, 'r') as caddy_file:
                file_content = caddy_file.read()
                if file_content:
                    self.config = json.loads(file_content)
        self.deploy_config()

    @staticmethod
    def default_config():
        return {
            "apps": {
                "http": {
                    "servers": {}
                }
            }
        }

    def add_custom_domain(self, domain, port=HTTPS_PORT):
        config = reverse_proxy_config(domain, port, self.main_domain_host, self.main_domain_port)

        site_name = f"{domain}:{port}"
        self.config['apps']['http']['servers'][site_name] = config

        success = self.deploy_config()

        with open(self.config_json_file, 'w') as outfile:
            outfile.write(json.dumps(self.config, indent=True))

        return success

    def remove_custom_domain(self, domain, port=HTTPS_PORT):
        site_name = f"{domain}:{port}"
        del self.config['apps']['http']['servers'][site_name]

    def deploy_config(self):
        headers = {'Content-Type': 'application/json'}
        load_url = f"{self.admin_url}/load"
        response = self.do_post_request(load_url, headers=headers, content=json.dumps(self.config))

        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=str(response.text))
        return response.text

    def deployed_config(self):
        config_url = f"{self.admin_url}/config/"
        response = self.do_get_request(config_url)
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=str(response.text))
        return response.json()

    def domain_config(self, host, port):
        id_url = f"{self.admin_url}/id/{host}:{port}"
        response = self.do_get_request(id_url)
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=str(response.text))
        return response.json()

    def do_get_request(self, url, **kwargs):
        try:
            return httpx.get(url, **kwargs)
        except Exception as ex:
            status_code = 500
            if hasattr(ex, 'status_code'):
                status_code = ex.status_code
            raise HTTPException(status_code=status_code, detail=str(ex))

    def do_post_request(self, url, **kwargs):
        try:
            return httpx.post(url, **kwargs)
        except Exception as ex:
            status_code = 500
            if hasattr(ex, 'status_code'):
                status_code = ex.status_code
            raise HTTPException(status_code=status_code, detail=str(ex))


caddy_server = Caddy()
