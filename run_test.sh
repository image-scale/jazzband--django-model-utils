#!/usr/bin/env bash
set -eo pipefail

export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
export CI=true
export SQLITE=1

rm -rf .pytest_cache

python -m pytest tests/ -v --tb=short --no-header -p no:cacheprovider --no-cov

