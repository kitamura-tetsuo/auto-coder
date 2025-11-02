#!/bin/bash
set -e

echo "🔄 Syncing dependencies with uv..."
uv sync --dev --extra test

echo ""
echo "🧪 Running tests with coverage..."
uv run pytest --cov=src/auto_coder --cov-report=term-missing

echo ""
echo "📝 Running linting checks..."

echo "  ✓ Running black (check)..."
uv run black --check src/ tests/

echo "  ✓ Running isort (check)..."
uv run isort --check-only src/ tests/

echo "  ✓ Running flake8..."
uv run flake8 src/ tests/

echo ""
echo "🔍 Running type checking..."
uv run mypy src/

echo ""
echo "✅ All checks passed!"
