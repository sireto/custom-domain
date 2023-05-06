from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.openapi.models import APIKey

from app.caddy.caddy import caddy_server
from app.security import get_api_key

"""
Domain API
===========
GET     /domains
POST    /domains?domain=<domain>&upstream=<upstream> 
DELETE  /domains?domain=<domain>
"""
domain_api = APIRouter()


@domain_api.get("/domains", tags=["Custom Domain API"])
async def get_domains(api_key: APIKey = Depends(get_api_key)):
    return caddy_server.list_domains()


@domain_api.post("/domains", tags=["Custom Domain API"])
async def add_domain(domain: str,
                     upstream: Optional[str] = None,
                     api_key: APIKey = Depends(get_api_key)):
    caddy_server.add_custom_domain(domain, upstream)
    return "OK"


@domain_api.delete("/domains", tags=["Custom Domain API"])
async def remove_domains(domain: str,
                         api_key: APIKey = Depends(get_api_key)):
    caddy_server.remove_custom_domain(domain)
    return "OK"
