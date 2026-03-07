#!/bin/bash

echo "=== Starting gunicorn ==="
exec gunicorn sv94:app --bind 0.0.0.0:${PORT:-10000} --timeout 120 --workers 1
