#!/usr/bin/env bash
# Optional step of the upstream data-app-python-js entrypoint sequence: runs
# once after the app repo is cloned into /app, before Supervisord starts.
set -euo pipefail
cd "$(dirname "$0")/.."

npm install
npm run build
