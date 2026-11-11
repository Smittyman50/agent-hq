# Agent HQ MQTT publisher: lets agents publish status and commands to your broker, as an MCP server.
FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastmcp>=2" "aiomqtt>=2.3"
COPY mqtt_publisher_mcp.py .
RUN useradd --system --no-create-home publisher
USER publisher
EXPOSE 8810
CMD ["python", "mqtt_publisher_mcp.py"]
