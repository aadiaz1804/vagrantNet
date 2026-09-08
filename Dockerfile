FROM python:3.13-slim

# tini so SIGTERM reaches the daemon: it drops DTR on the way out, and a radio
# left with DTR asserted ignores the next connect.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY vagrantnet ./vagrantnet
RUN pip install --no-cache-dir .

# Seed content, copied into the volume on first run only.
COPY examples /app/examples

COPY docker/entrypoint.py /app/entrypoint.py

# Pages, boards and config live here. Mount it or lose your board.
VOLUME /data
ENV VN_DATA=/data

ENTRYPOINT ["/usr/bin/tini", "--", "python", "/app/entrypoint.py"]
