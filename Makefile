.PHONY: test

test:
	uv run pytest tests/*

llm:
	cd parselbox && uvx repo2txt --ignore-types .pyc .lock