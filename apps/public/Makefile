.PHONY: check api-check ui-check audit browser

check: api-check ui-check

api-check:
	api/.venv/bin/bandit -q -r api/src -ll -ii
	api/.venv/bin/ruff check api/src api/migrations api/tests local.py run.py seed_users.py wait_local.py
	api/.venv/bin/ruff format --check api/src api/migrations api/tests local.py run.py seed_users.py wait_local.py
	PYTHONPATH=../../src api/.venv/bin/mypy api/src
	PYTHONPATH=../../src api/.venv/bin/python ../../scripts/verify_no_network.py pytest -q api/tests

ui-check:
	cd ui && npm run format:check && npm run build

audit:
	@aqa_requirements=$$(mktemp); \
	trap 'rm -f "$$aqa_requirements"' EXIT; \
	uv export --project api --locked --no-dev --no-emit-project --format requirements-txt --output-file "$$aqa_requirements" >/dev/null && \
	api/.venv/bin/pip-audit --requirement "$$aqa_requirements" --require-hashes --disable-pip --progress-spinner=off
	cd ui && npm audit

browser:
	cd ui && npm run test:e2e
