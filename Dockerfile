FROM tiangolo/uvicorn-gunicorn-fastapi:python3.8
RUN apt update
RUN apt install -y debian-keyring debian-archive-keyring apt-transport-https
RUN curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
RUN curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
RUN apt update && apt install -y caddy

COPY ./app /app/app
COPY ./domains /app/domains
COPY ./requirements.txt /app/requirements.txt
COPY ./entrypoint.sh /app/entrypoint.sh

WORKDIR /app
RUN  pip3 install -r requirements.txt

CMD ["/app/entrypoint.sh"]
