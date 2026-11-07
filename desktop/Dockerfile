# One of these per agent that needs a desktop.
# linuxserver/webtop = Ubuntu + XFCE, viewable in a browser on port 3000 (https on 3001).
#
# Three things run on the desktop besides XFCE:
#   agent-browser   a Chromium window (persistent profile in /config/agent-browser) that you
#                   can see and use, with Chrome DevTools Protocol on 127.0.0.1:9222
#   playwright-mcp  MCP server on :8931 that drives that same Chromium through the page's
#                   accessibility tree: text-only web browsing, no vision model needed
#   desktop-mcp     MCP server on :8765 for screenshots, mouse and keyboard on the whole desktop
FROM lscr.io/linuxserver/webtop:ubuntu-xfce

ARG PLAYWRIGHT_MCP_VERSION=0.0.84
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

RUN apt-get update \
 && apt-get install -y --no-install-recommends xdotool xdg-utils python3-venv curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @playwright/mcp@${PLAYWRIGHT_MCP_VERSION} \
 && npx -y playwright@$(node -p "require('/usr/lib/node_modules/@playwright/mcp/node_modules/playwright/package.json').version") install --with-deps chromium \
 && chmod -R a+rX ${PLAYWRIGHT_BROWSERS_PATH} \
 && rm -rf /var/lib/apt/lists/* /root/.npm

RUN python3 -m venv /opt/desktop/venv \
 && /opt/desktop/venv/bin/pip install --no-cache-dir "fastmcp>=2" pillow

COPY desktop_mcp.py /opt/desktop/desktop_mcp.py
COPY agent-browser /usr/local/bin/agent-browser
COPY services/ /custom-services.d/
RUN chmod +x /usr/local/bin/agent-browser /custom-services.d/*

EXPOSE 3000 3001 8765 8931
