# Proofread episode sandbox. Run with --runtime=runsc --read-only --network none (see runner.py).
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git procps \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "pytest==8.3.5"
RUN useradd -u 1000 -M -d /tmp -s /bin/bash agent \
 && git config --system user.email agent@sandbox.local \
 && git config --system user.name agent \
 && git config --system init.defaultBranch main \
 && git config --system --add safe.directory '*'
COPY snap.py /opt/proofread/snap.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 HOME=/tmp LANG=C.UTF-8
WORKDIR /workspace
