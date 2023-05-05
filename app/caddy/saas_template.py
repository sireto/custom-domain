from typing import Dict, List

HTTPS_PORT = 443


def https_template(port=HTTPS_PORT, disable_https=False):
    return {
        "apps": {
            "http": {
                "servers": {
                    f"{port}": {
                        "listen": [
                            f":{port}"
                        ],
                        "automatic_https": {"disable": disable_https},
                        "routes": []
                    }
                }
            }
        }
    }


class DomainAlreadyExists(ValueError):
    pass


class DomainDoesNotExist(ValueError):
    pass


def add_https_domain(domain, upstream, port=HTTPS_PORT, template=None, replace=True, disable_https=False):
    if not template or not isinstance(template, dict):
        template = {}
    else:
        template = template.copy()

    apps = template.get("apps", None)
    if not apps:
        apps = {}
        template["apps"] = apps

    http = apps.get("http", None)
    if not http:
        http = {}
        apps["http"] = http

    servers = http.get("servers", None)
    if not servers:
        servers = {}
        http["servers"] = servers

    https_server = servers.get(f"{port}", None)
    if not https_server:
        https_server = {"listen": [f":{port}"], "routes": []}
        servers[f"{port}"] = https_server

    routes: List[Dict] = https_server.get("routes", [])
    expected_route = route_template(domain, upstream, disable_https=disable_https)
    exists = False
    for route in routes:
        for match in route.get("match", []):
            if domain in match.get("host", []):
                exists = True
                if replace:
                    # replace the handle with expected route handle
                    route["handle"] = expected_route["handle"]
                    break
                raise DomainAlreadyExists(f"{domain} already exists")

    if not exists:
        routes.append(expected_route)

    return template


def route_template(domain, upstream, disable_https=False):
    return {
        "handle": [
            {
                "handler": "subroute",
                "routes": [
                    {
                        "handle": [
                            reverse_proxy_handle_template(upstream, disable_https=disable_https)
                        ]
                    }
                ]
            }
        ],
        "match": [
            {
                "host": [domain]
            }
        ],
        "terminal": True
    }


def reverse_proxy_handle_template(upstream, disable_https=False, handle_id=None):
    if ":" not in upstream:
        upstream = f"{upstream}:{HTTPS_PORT}"

    handle = {
        "handler": "reverse_proxy",
        "headers": {
            "request": {
                "set": {
                    "Host": [
                        "{http.reverse_proxy.upstream.host}"
                    ],
                    "X-Real-Ip": [
                        "{http.reverse-proxy.upstream.address}"
                    ]
                }
            }
        },
        "transport": {
            "protocol": "http",
            "tls": {}
        },
        "upstreams": [
            {
                "dial": upstream
            }
        ]
    }

    if disable_https:
        del handle["transport"]["tls"]

    if handle_id:
        handle["@id"] = handle_id

    return handle


def delete_https_domain(domain, template, port=HTTPS_PORT):
    try:
        template = template.copy()
        routes = template["apps"]["http"]["servers"][f"{port}"]["routes"]

        removed = False
        for route in routes.copy():
            for match in route.get("match", []):
                if domain in match.get("host", []):
                    routes.remove(route)
                    removed = True

        if not removed:
            raise DomainDoesNotExist(f"{domain} does not exist")

        template["apps"]["http"]["servers"][f"{port}"]["routes"] = routes
        return template

    except KeyError:
        raise
    except IndexError:
        raise


def list_domains(template, port=HTTPS_PORT):
    try:
        domains = []

        routes = template["apps"]["http"]["servers"][f"{port}"]["routes"]
        for route in routes:
            for match in route.get("match", []):
                for host in match.get("host", []):
                    domains.append(host)

        return domains
    except KeyError:
        return []
    except IndexError:
        return []
