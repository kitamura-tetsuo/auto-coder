# Makefile for comprehensive type checking

.PHONY: type-check type-check-fast type-check-detailed install-type-tools clean

# Install type checking tools
install-type-tools:
	@echo "Installing type checking tools..."
	pip install mypy pylint types-setuptools
	npm install -g pyright@1.1.407

# Fast type check (exit on first error)
type-check-fast:
	@echo "=== Fast Type Check ==="
	npx pyright src/auto_coder/automation_engine.py

# Detailed type check with multiple tools
type-check:
	@echo "=== Comprehensive Type Check ==="
	python comprehensive_type_checker.py

# Detailed type check on specific file
type-check-file:
	@echo "=== File-specific Type Check ==="
	npx pyright src/auto_coder/automation_engine.py --outputjson

# CI-ready type check
type-check-ci:
	@echo "=== CI Type Check ==="
	npx pyright src/ --outputjson > pyright_results.json
	ERROR_COUNT=$$(npx pyright src/ 2>&1 | grep -c "error:"); \
	WARNING_COUNT=$$(npx pyright src/ 2>&1 | grep -c "warning:"); \
	echo "Pyright errors: $$ERROR_COUNT"; \
	echo "Pyright warnings: $$WARNING_COUNT"; \
	if [ $$ERROR_COUNT -gt 0 ]; then \
		echo "❌ Type checking failed"; \
		npx pyright src/ 2>&1 | grep -E "(error|warning)" | head -20; \
		exit 1; \
	else \
		echo "✅ Type checking passed"; \
	fi

# Clean artifacts
clean:
	rm -f pyright_results.json
	rm -f .mypy_cache
	rm -rf .pytest_cache

# Show help
help:
	@echo "Available targets:"
	@echo "  install-type-tools  - Install all type checking tools"
	@echo "  type-check-fast     - Quick check on automation_engine.py"
	@echo "  type-check          - Comprehensive type checking"
	@echo "  type-check-file     - Detailed file check"
	@echo "  type-check-ci       - CI-ready type checking"
	@echo "  clean              - Clean artifacts"
	@echo "  help               - Show this help"