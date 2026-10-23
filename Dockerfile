# trading-lab runtime image: the agents, paper trading and the CLI.
# The AI research swarm runs on your own machine instead (it needs your Claude login).
#
#   docker build -t trading-lab .
#
# NautilusTrader ships wheels for linux x86_64 and aarch64 (glibc >= 2.34), so this
# builds on a PC, an ARM VPS or a Raspberry Pi 5 (64-bit OS).

FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/New_York

RUN apt-get update && apt-get install -y --no-install-recommends libseccomp2 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin lab
WORKDIR /lab

# Dependencies first (cached layer), then the code.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && python -c "import lab, nautilus_trader; print('nautilus', nautilus_trader.__version__)"

COPY lab.toml ./lab.toml
RUN mkdir -p data results research && chown -R lab:lab /lab
USER lab

HEALTHCHECK --interval=60s --timeout=20s --start-period=180s --retries=3 \
    CMD ["lab", "health", "--max-age", "180"]

ENTRYPOINT ["lab"]
CMD ["doctor"]
