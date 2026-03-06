#!/bin/bash
echo "=== Installing Playwright Chromium ==="
playwright install chromium 2>&1 || echo "Playwright install warning (may already exist)"
echo "=== Starting gunicorn ==="
exec gunicorn sv94:app --bind 0.0.0.0:$PORT --timeout 120 --workers 1
