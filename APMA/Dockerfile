FROM python:3.12-slim

# Create application user and directory
RUN useradd --create-home --no-log-init --shell /bin/bash appuser
WORKDIR /opt/ampa
ENV PATH="/opt/ampa/.venv/bin:$PATH"

# Install system deps and cleanup
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create virtualenv and install python deps later via requirements.txt
RUN python -m venv .venv

COPY requirements.txt ./
RUN .venv/bin/pip install --upgrade pip setuptools wheel \
    && .venv/bin/pip install -r requirements.txt

COPY . /opt/ampa
RUN chown -R appuser:appuser /opt/ampa
USER appuser

EXPOSE 8080
CMD ["/opt/ampa/.venv/bin/python", "-u", "./ampa_daemon.py"]
