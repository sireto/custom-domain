from fastapi import APIRouter, Depends
from fastapi.openapi.models import APIKey

from app.caddy.caddy import caddy_server
from app.security import get_api_key

"""
Domain API
===========
GET     /domains?host=domain&port=443
POST    /domains?host=domain&port=443 
DELETE  /domains?host=domain&port=443
"""
domain_api = APIRouter()


@domain_api.get("/domains", tags=["Domain API"])
async def get_domains(api_key: APIKey = Depends(get_api_key)):
    return caddy_server.list_domains()


@domain_api.get("/domains/config", tags=["Domain API"])
async def get_config(api_key: APIKey = Depends(get_api_key)):
    return caddy_server.deployed_config()


@domain_api.post("/domains", tags=["Domain API"])
async def add_domain(host: str, api_key: APIKey = Depends(get_api_key)):
    caddy_server.add_custom_domain(host)
    return "OK"


@domain_api.delete("/domains", tags=["Domain API"])
async def remove_domains(host: str, api_key: APIKey = Depends(get_api_key)):
    caddy_server.remove_custom_domain(host)
    return "OK"
