def https_template(upstream):
    return {
        "apps": {
            "http": {
                "servers": {
                    "srv0": {
                        "listen": [
                            ":443"
                        ],
                        "routes": [
                            sub_route_template(upstream)
                        ]
                    }
                }
            }
        }
    }


def sub_route_template(upstream):
    return {
        "handle": [
            {
                "handler": "subroute",
                "routes": [
                    {
                        "handle": [
                            reverse_proxy_handle_template(upstream)
                        ]
                    }
                ]
            }
        ],
        "match": [
            {
                "host": []
            }
        ],
        "terminal": True
    }


def reverse_proxy_handle_template(upstream):
    return {
        "@id": upstream,
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


def add_domains(template, domains):
    try:
        match_config = template["apps"]["http"]["servers"]["srv0"]["routes"][0]["match"]
        if match_config:
            match_hosts = match_config[0].get("host", [])
            for domain in domains:
                if domain not in match_hosts:
                    match_hosts.append(domain)
            match_config[0]["host"] = match_hosts
            template["apps"]["http"]["servers"]["srv0"]["routes"][0]["match"] = match_config
            return template
    except KeyError:
        raise
    except IndexError:
        raise


def delete_domains(template, domains):
    try:
        match_config = template["apps"]["http"]["servers"]["srv0"]["routes"][0]["match"]
        if match_config:
            match_hosts = match_config[0].get("host", [])
            for domain in domains:
                if domain in match_hosts:
                    match_hosts.remove(domain)
            match_config[0]["host"] = match_hosts
            template["apps"]["http"]["servers"]["srv0"]["routes"][0]["match"] = match_config
            return template
    except KeyError:
        raise
    except IndexError:
        raise


def list_domains(template):
    try:
        match_config = template["apps"]["http"]["servers"]["srv0"]["routes"][0]["match"]
        if match_config:
            match_hosts = match_config[0].get("host", [])
            return match_hosts
    except KeyError:
        return []
    except IndexError:
        return []
