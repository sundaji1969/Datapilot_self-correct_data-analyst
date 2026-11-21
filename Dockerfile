# DataPilot code sandbox: build once with  docker build -t datapilot-sandbox .
# Slow/unstable network inside Docker? Download the wheels on the host first (see README) into ./wheels —
# pip then installs offline from that folder; without it, pip downloads from PyPI.
FROM python:3.12-slim
ENV PYTHONUTF8=1 MPLBACKEND=Agg PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=10 PIP_NO_CACHE_DIR=1
ARG PKGS="pandas==3.1.1 numpy==2.5.5 matplotlib==3.11.2 openpyxl==3.1.5"
RUN --mount=type=bind,target=/ctx \
    if [ -d /ctx/wheels ]; then pip install --no-index --find-links /ctx/wheels $PKGS; else pip install $PKGS; fi \
 && useradd --create-home sandbox
USER sandbox
WORKDIR /workspace
