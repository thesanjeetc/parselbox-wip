.PHONY: test

test:
	uv run pytest tests/*

check:
	uv run ruff check --fix
	uv run ruff format

llm:
	cd parselbox && uvx repo2txt --ignore-types .pyc .lock