#!/bin/bash

export PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/pw-browsers

echo "=== Installing Playwright Chromium ==="
playwright install --with-deps chromium 2>&1 || playwright install chromium 2>&1 || echo "WARNING: playwright install failed"

echo "=== Starting gunicorn ==="
exec gunicorn sv94:app --bind 0.0.0.0:${PORT:-10000} --timeout 300 --workers 1
