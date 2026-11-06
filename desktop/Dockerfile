# One of these per agent that needs a desktop.
# linuxserver/webtop = Ubuntu + XFCE, viewable in a browser on port 3000 (https on 3001).
FROM lscr.io/linuxserver/webtop:ubuntu-xfce

RUN apt-get update \
 && apt-get install -y --no-install-recommends xdotool xdg-utils python3-venv \
 && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/desktop/venv \
 && /opt/desktop/venv/bin/pip install --no-cache-dir "fastmcp>=2" pillow

COPY desktop_mcp.py /opt/desktop/desktop_mcp.py

# linuxserver images run anything in /custom-services.d as an s6 service.
# Run the MCP server as the desktop user so it can talk to the X display.
RUN mkdir -p /custom-services.d && printf '%s\n' \
  '#!/usr/bin/with-contenv bash' \
  'until [ -e /tmp/.X11-unix/X1 ]; do sleep 1; done' \
  'export DISPLAY=:1 HOME=/config' \
  'exec s6-setuidgid abc /opt/desktop/venv/bin/python /opt/desktop/desktop_mcp.py' \
  > /custom-services.d/desktop-mcp && chmod +x /custom-services.d/desktop-mcp

EXPOSE 3000 3001 8765
