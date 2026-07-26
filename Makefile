.PHONY: test lint validate

test:
	uv run --project harbor pytest

lint:
	uv run --project harbor ruff check harbor/src harbor/tests

validate:
	uv run --project harbor harbor swe-touch validate-data data/v0.1.0
