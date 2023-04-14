#!/bin/bash
docker login git.sireto.io:5050
docker build -t git.sireto.io:5050/https/https .
docker push git.sireto.io:5050/https/https
