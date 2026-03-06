#!/bin/bash

export PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/pw-browsers

echo "=== Installing Playwright Chromium + system deps ==="
playwright install --with-deps chromium 2>&1 || playwright install chromium 2>&1 || echo "WARNING: playwright install failed"

echo "=== Verifying browser ==="
find $PLAYWRIGHT_BROWSERS_PATH -type f -name "chrome*" 2>/dev/null | head -3
python -c "
import os; os.environ['PLAYWRIGHT_BROWSERS_PATH']='/opt/render/.cache/pw-browsers'
from playwright.sync_api import sync_playwright
p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()
print('OK: Playwright chromium works')
" 2>&1 || echo "WARNING: browser test failed (will retry at runtime)"

echo "=== Starting gunicorn ==="
exec gunicorn sv94:app --bind 0.0.0.0:${PORT:-10000} --timeout 120 --workers 1
