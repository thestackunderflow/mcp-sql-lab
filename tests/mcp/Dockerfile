# Phase 2: the two MCP servers (stdio) plus a Python MCP client for the tests. Versions pinned (lab snapshot October 2026).
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
# Postgres MCP Pro 0.3.0 accepts any MCP SDK >= 1.5; keep the 1.x line it was built against
RUN pip install --no-cache-dir "postgres-mcp==0.3.0" "mcp[cli]>=1.5,<2" "pytest>=8,<9" "psycopg[binary]>=3.2,<3.3"
# Google MCP Toolbox 1.14.0 (the npm package fetches the server binary)
RUN npm install -g @toolbox-sdk/server@1.14.0 && npm cache clean --force
