from fastapi import APIRouter

from app.caddy.caddy import caddy_server, HTTPS_PORT

"""
Domain API
===========
GET     /domains?host=domain&port=443
POST    /domains?host=domain&port=443 
DELETE  /domains?host=domain&port=443
"""
domain_api = APIRouter()


@domain_api.get("/domains", tags=["Domain API"])
async def get_domains(host: str = None, port: int = None):
    if host:
        port = port or HTTPS_PORT
        return caddy_server.domain_config(host, port)
    return caddy_server.deployed_config()


@domain_api.post("/domains", tags=["Domain API"])
async def add_domain(host: str, port: int):
    return caddy_server.add_custom_domain(host, port=port)


@domain_api.delete("/domains", tags=["Domain API"])
async def remove_domains(host: str, port: int):
    return caddy_server.remove_custom_domain(host, port=port)



