.PHONY: lint test integration-test docs tui-demo package-smoke verify

lint:
	uv run ruff check src tests scripts
	uv run mypy src

test:
	uv run pytest tests/unit -v

integration-test:
	uv run pytest tests/integration -v

docs:
	uv run python scripts/gen_protocol_doc.py

tui-demo:
	uv run python scripts/capture_tui_demo.py --output docs/images/coderook-tui.svg

package-smoke:
	uv build
	uv run python scripts/smoke_wheel.py dist

verify:
	uv sync --frozen
	uv run ruff check .
	uv run python scripts/check_brand.py
	uv run python scripts/check_public_repo.py
	uv run mypy src
	uv run mypy --platform linux src
	uv run pytest -q
	uv run python scripts/gen_protocol_doc.py --check
	uv build
	uv run python scripts/smoke_wheel.py dist
