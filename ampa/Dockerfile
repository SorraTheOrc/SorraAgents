FROM python:3.12-slim

# Use /opt/ampa as application directory. We avoid creating a named
# non-root user during image build (some rootless builders fail on useradd).
WORKDIR /opt/ampa
ENV PATH="/opt/ampa/.venv/bin:$PATH"

# Install system deps and cleanup
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create virtualenv and install python deps
RUN python -m venv .venv

COPY requirements.txt ./
RUN .venv/bin/pip install --upgrade pip setuptools wheel \
    && .venv/bin/pip install -r requirements.txt

# Copy application and make files writable by a non-root UID. We avoid
# calling `useradd` to remain compatible with rootless builders; instead we
# set ownership to UID 1000 and instruct the container to run as that UID.
COPY . /opt/ampa
RUN chown -R 1000:1000 /opt/ampa

# Run as UID 1000 (no named user required). Consumers may override with
# `--user` if they need a different UID/GID mapping.
USER 1000

EXPOSE 8080
CMD ["/opt/ampa/.venv/bin/python", "-u", "./ampa_daemon.py"]
