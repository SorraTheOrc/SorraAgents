FROM python:3.12-slim

# Use /opt/ampa as application directory. We avoid creating a named
# non-root user during image build (some rootless builders fail on useradd).
WORKDIR /opt/ampa
ENV PATH="/opt/ampa/.venv/bin:$PATH"
# Ensure Python can import the `ampa` package located at /opt/ampa by adding
# the parent directory (/opt) to PYTHONPATH. Previously this pointed to
# /opt/ampa which caused Python to look for /opt/ampa/ampa and fail.
ENV PYTHONPATH="/opt"

# Install system deps and cleanup
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create virtualenv and install python deps
RUN python -m venv .venv

# Copy requirements first (to leverage build cache) then copy the application
# source so we can pip-install the package into the venv. Only install the
# package if a pyproject.toml or setup.py is present to avoid failing builds
# for codebases that don't provide an installable layout.
COPY requirements.txt ./
RUN .venv/bin/pip install --upgrade pip setuptools wheel \
    && .venv/bin/pip install -r requirements.txt

# Copy application sources so we can install the package into the venv
COPY . /opt/ampa
RUN if [ -f pyproject.toml ] || [ -f setup.py ]; then \
      .venv/bin/pip install --no-build-isolation --no-deps -e .; \
    fi

# Make files writable by a non-root UID. We avoid calling `useradd` to remain
# compatible with rootless builders; instead we set ownership to UID 1000 and
# instruct the container to run as that UID.
RUN chown -R 1000:1000 /opt/ampa

# Run as UID 1000 (no named user required). Consumers may override with
# `--user` if they need a different UID/GID mapping.
USER 1000

EXPOSE 8080
# Run the package module so we use the package entrypoint (ampa.daemon)
# Using the module ensures the correct file inside the package is executed.
CMD ["/opt/ampa/.venv/bin/python", "-u", "-m", "ampa.daemon"]
