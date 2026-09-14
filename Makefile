.DEFAULT_GOAL := help
PY := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: help venv install migrate seed knowledge run test cov lint fmt calibrate demo docker-up docker-down docker-logs clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv
	python3.12 -m venv .venv && $(PIP) install --upgrade pip

install: venv ## Install development dependencies
	$(PIP) install -r requirements/dev.txt

migrate: ## Apply database migrations
	$(PY) manage.py migrate

seed: ## Load realistic demo orders, payments and events
	$(PY) manage.py seed_demo_data --flush

knowledge: ## Ingest SOP/policy documents into pgvector
	$(PY) manage.py ingest_knowledge

calibrate: ## Measure knowledge-base retrieval quality
	$(PY) manage.py calibrate_knowledge

run: ## Run the development server
	$(PY) manage.py runserver 8000

test: ## Run the test suite
	$(PY) -m pytest

cov: ## Run tests with a coverage report
	$(PY) -m pytest --cov=apps --cov-report=term-missing

lint: ## Lint the codebase
	.venv/bin/ruff check apps config tests

fmt: ## Auto-fix lint issues and format
	.venv/bin/ruff check --fix apps config tests
	.venv/bin/ruff format apps config tests

demo: ## Run the scripted end-to-end demo against a live server
	$(PY) scripts/demo.py

docker-up: ## Build and start the full stack
	docker compose up --build -d

docker-down: ## Stop the stack and remove volumes
	docker compose down -v

docker-logs: ## Tail application logs
	docker compose logs -f web

clean: ## Remove caches and build artefacts
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
