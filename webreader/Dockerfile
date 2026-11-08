# Agent HQ web reader: text-only web tools (feeds, pages, search) as an MCP server.
FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastmcp>=2" httpx feedparser trafilatura
COPY webreader_mcp.py .
RUN useradd --system --no-create-home reader
USER reader
EXPOSE 8800
CMD ["python", "webreader_mcp.py"]
