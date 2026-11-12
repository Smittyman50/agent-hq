# Agent HQ lab tools: one image for any number of lab tool MCP servers (each instance
# picks its integrations, read or write access, and its own credentials).
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends iputils-ping \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY labtools ./labtools
RUN useradd --system --uid 10001 --no-create-home labtools   # fixed UID: owns the mounted SSH key
USER labtools
# Configure each instance with LABTOOLS_INTEGRATIONS, LABTOOLS_ACCESS and LABTOOLS_NAME.
EXPOSE 8820
CMD ["python", "-m", "labtools"]
