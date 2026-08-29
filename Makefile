VENV = .venv
PYTHON = $(VENV)/bin/python

.PHONY: help install run test lint format clean

# ── Default ──────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "Usage: make <target>"
	@echo ""
	@echo "  install  Create .venv and install all dependencies"
	@echo "  run      Run the API locally with autoreload (TR_CONFIG_PATH=./data/config.yml)"
	@echo "  test     Run the test suite with coverage"
	@echo "  lint     Check lint and formatting (ruff check + format --check)"
	@echo "  format   Auto-fix formatting and lint (ruff format + check --fix)"
	@echo "  clean    Remove __pycache__, .pytest_cache, .coverage, coverage.json"
	@echo ""

# ── Dev ───────────────────────────────────────────────────────────────────────

install:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -r requirements-dev.txt

run:
	TR_CONFIG_PATH=$${TR_CONFIG_PATH:-$(CURDIR)/data/config.yml} \
		$(VENV)/bin/uvicorn tr_bridge.main:app --host 127.0.0.1 --port 8000 --reload

test:
	$(PYTHON) -m pytest --cov=tr_bridge --cov-report=term-missing

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format:
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff check --fix .

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .coverage coverage.json htmlcov
