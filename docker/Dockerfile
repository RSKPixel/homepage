# Same Ubuntu release as the host, so journalctl and apt can read the host's
# journal and package state mounted at /host.
FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# python3 (server), figlet (banner), iproute2 (ss), procps (ps),
# util-linux (findmnt, lsblk), systemd (journalctl, timedatectl, loginctl)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 figlet iproute2 procps util-linux systemd ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# docker CLI only; talks to the host daemon through the mounted socket
COPY --from=docker:29-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app
COPY server.py .
COPY public/ public/

ENV HOST_ROOT=/host \
    HOMEPAGE_PORT=8088

EXPOSE 8088

CMD ["python3", "server.py"]
