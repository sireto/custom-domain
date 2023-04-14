#!/bin/bash

/usr/bin/caddy start
/usr/local/bin/uvicorn app.main:app --host 0.0.0.0 --port 9000