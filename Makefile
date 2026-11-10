.PHONY: install lint format test run dry-run status clean

install:
	python -m pip install -e ".[dev]"

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .

test:
	pytest

run:
	website-change-monitor run

dry-run:
	website-change-monitor run --dry-run

status:
	website-change-monitor status

clean:
	rm -rf build dist src/*.egg-info .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
