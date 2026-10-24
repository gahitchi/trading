# trading-lab runtime image: the agents, paper trading and the CLI.
# The AI research swarm runs on your own machine instead (it needs your Claude login).
#
#   docker build -t trading-lab .
#
# NautilusTrader ships wheels for linux x86_64 and aarch64 (glibc >= 2.34), so this
# builds on a PC, an ARM VPS or a Raspberry Pi 5 (64-bit OS).

FROM python:3.13-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/New_York

# Freeze Debian package resolution as well as the parent image and Python graph.
RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261001T000000Z bookworm main' > /etc/apt/sources.list \
    && apt-get update && apt-get install -y --no-install-recommends libseccomp2=2.5.4-1+deb12u1 \
    && dpkg-query -W > /usr/local/share/trading-lab-os-packages.txt \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin lab
WORKDIR /lab

# Dependencies first (cached layer), then the code.
COPY pyproject.toml README.md requirements.lock requirements-build.lock ./
COPY src ./src
RUN pip install --require-hashes -r requirements.lock -r requirements-build.lock && pip install --no-deps --no-build-isolation . && python -c "import lab, nautilus_trader; print('nautilus', nautilus_trader.__version__)"

COPY lab.toml ./lab.toml
RUN mkdir -p data results research && chown -R lab:lab /lab
USER lab

HEALTHCHECK --interval=60s --timeout=20s --start-period=180s --retries=3 \
    CMD ["lab", "health", "--max-age", "180"]

ENTRYPOINT ["lab"]
CMD ["doctor"]
