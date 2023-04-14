def reverse_proxy_config(source_host, source_port, target_host, target_port):
    return {
        "listen": [
            f":{source_port}"
        ],
        "routes": [
            {
                "handle": [
                    {
                        "handler": "subroute",
                        "routes": [
                            {
                                "handle": [
                                    {
                                        "@id": f"{source_host}:{source_port}",
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
                                                "dial": f"{target_host}:{target_port}"
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ],
                "match": [
                    {
                        "host": [
                            f"{source_host}"
                        ]
                    }
                ],
                "terminal": True
            }
        ]
    }

