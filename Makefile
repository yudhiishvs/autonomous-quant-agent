UV ?= uv
PAPER_CONFIG ?= configs/paper.yaml
OBSERVER_CONFIG ?= configs/observer.yaml
BACKTEST_CONFIG ?= configs/backtest.yaml
REPLAY_CONFIG ?= configs/replay.yaml

.DEFAULT_GOAL := help
.NOTPARALLEL:

.PHONY: help install format lint typecheck test coverage check security precommit package \
	compose-config doctor legacy-doctor demo benchmark check-paper-environment backtest replay \
	observe paper-dry-run paper-run status reconcile halt resume report dashboard backup-database \
	docker-build offline-up offline-down docker-observe docker-paper clean-generated
.PHONY: bootstrap build format-check unit integration regression freeze package-smoke \
	test-unit test-integration test-regression lock-check postgres-preflight coverage-offline coverage-postgres coverage-report

help:
	@echo "Autonomous Quant Agent — offline and paper-only targets"
	@echo "  install format lint typecheck test coverage check security precommit package"
	@echo "  doctor demo benchmark compose-config backtest replay"
	@echo "  check-paper-environment legacy-doctor observe paper-dry-run"
	@echo "  status reconcile halt resume report dashboard backup-database"
	@echo "  docker-build offline-up offline-down docker-observe docker-paper clean-generated"
	@echo "Default operation is credential-free and never submits an order."

bootstrap: install

install:
	$(UV) sync --locked --all-extras

format:
	$(UV) run --no-sync python scripts/format_non_ai.py

format-check:
	$(UV) run --no-sync ruff format --check .

lint:
	$(UV) run --no-sync ruff format --check .
	$(UV) run --no-sync ruff check .

typecheck:
	$(UV) run --no-sync mypy src docker

test:
	env -u APA_TEST_POSTGRES_URL -u APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE -u APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES -u APA_TEST_POSTGRES_CONTAINER \
		$(UV) run --no-sync python scripts/verify_no_network.py pytest -q -m 'not postgres'

unit:
	env -u APA_TEST_POSTGRES_URL -u APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE -u APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES -u APA_TEST_POSTGRES_CONTAINER \
		$(UV) run --no-sync python scripts/verify_no_network.py pytest -q -m 'not integration'

postgres-preflight:
	@test -n "$(APA_TEST_POSTGRES_URL)" || (echo 'A disposable loopback collector_test PostgreSQL 16 URL is required.' >&2; exit 2)
	@test "$(APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE)" = "YES" || (echo 'Explicit disposable database reset acknowledgement is required.' >&2; exit 2)
	@test "$(APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES)" = "YES" || (echo 'Explicit disposable cluster role acknowledgement is required.' >&2; exit 2)
	@for tool in createdb dropdb pg_dump psql; do command -v "$$tool" >/dev/null || { echo "Required PostgreSQL verification utility is missing: $$tool" >&2; exit 2; }; done

integration: postgres-preflight
	$(UV) run --no-sync pytest -q -m postgres

test-unit: unit
test-integration: integration
test-regression: regression

lock-check:
	$(UV) lock --check

regression:
	$(UV) run --no-sync python scripts/verify_offline_commands.py regression

coverage: postgres-preflight
	$(MAKE) coverage-offline
	$(MAKE) coverage-postgres
	$(MAKE) coverage-report

coverage-offline:
	env -u APA_TEST_POSTGRES_URL -u APA_TEST_POSTGRES_ALLOW_DESTRUCTIVE -u APA_TEST_POSTGRES_ALLOW_CLUSTER_ROLES -u APA_TEST_POSTGRES_CONTAINER \
		$(UV) run --no-sync python scripts/verify_no_network.py pytest -q -m 'not postgres' --cov=adaptive_trader --cov-branch --cov-report=

coverage-postgres: postgres-preflight
	$(UV) run --no-sync pytest -q -m postgres --cov=adaptive_trader --cov-branch --cov-append --cov-report=

coverage-report:
	$(UV) run --no-sync coverage report --fail-under=74
	$(UV) run --no-sync coverage report --include='src/adaptive_trader/platform/*' --fail-under=85
	$(UV) run --no-sync coverage xml
	$(UV) run --no-sync coverage html

freeze:
	$(UV) run --no-sync python scripts/verify_main_ai_freeze.py

check: freeze lock-check format-check lint typecheck coverage regression demo security package-smoke compose-config benchmark
	git diff --check
	$(UV) run --no-sync python scripts/verify_main_ai_freeze.py

security:
	$(UV) sync --locked --all-extras --group security
	$(UV) run --no-sync bandit -c pyproject.toml -r src docker -ll -ii
	$(UV) run --no-sync pytest -q tests/architecture tests/safety
	$(UV) run --no-sync pre-commit run --all-files --show-diff-on-failure
	$(UV) export --locked --all-extras --group security --no-emit-project --no-editable \
		--format requirements-txt --output-file /tmp/aqa-locked-security-requirements.txt
	$(UV) run --no-sync pip-audit --requirement /tmp/aqa-locked-security-requirements.txt \
		--require-hashes --disable-pip --progress-spinner=off

precommit:
	$(UV) sync --locked --all-extras --group security
	$(UV) run --no-sync pre-commit run --all-files --show-diff-on-failure

package:
	$(UV) build --out-dir dist

build: package

package-smoke:
	$(UV) run --no-sync python scripts/verify_clean_package.py

compose-config:
	docker compose --env-file .env.example -f docker-compose.yml config --quiet

check-paper-environment:
	./scripts/check_local_paper_environment.sh $(OBSERVER_CONFIG)

doctor:
	$(UV) run --no-sync aqa doctor --config-root configs

legacy-doctor:
	$(UV) run --no-sync python -m adaptive_trader.cli doctor --config $(OBSERVER_CONFIG)

demo:
	$(UV) run --no-sync python scripts/verify_offline_commands.py demo

benchmark:
	$(UV) run --no-sync python scripts/benchmark_pipeline.py \
		--warmups 2 --repeats 5 --iterations 25

backtest:
	$(UV) run --no-sync python -m adaptive_trader.cli backtest \
		--config $(BACKTEST_CONFIG) --synthetic

replay:
	$(UV) run --no-sync python -m adaptive_trader.cli replay --config $(REPLAY_CONFIG)

observe:
	./scripts/run_observer.sh $(OBSERVER_CONFIG)

paper-dry-run:
	$(UV) run --no-sync python -m adaptive_trader.cli paper-once \
		--config $(OBSERVER_CONFIG) --dry-run

paper-run:
	./scripts/run_paper.sh $(PAPER_CONFIG)

status:
	$(UV) run --no-sync python -m adaptive_trader.cli status --config $(OBSERVER_CONFIG)

reconcile:
	$(UV) run --no-sync python -m adaptive_trader.cli reconcile --config $(OBSERVER_CONFIG)

halt:
	@test -n "$(REASON)" || (echo 'Usage: make halt REASON="operator reason"' >&2; exit 2)
	$(UV) run --no-sync python -m adaptive_trader.cli halt \
		--config $(PAPER_CONFIG) --reason "$(REASON)"

resume:
	@test "$(ACK)" = "I_HAVE_REVIEWED_THE_PAPER_ACCOUNT" || \
		(echo 'Usage: make resume ACK=I_HAVE_REVIEWED_THE_PAPER_ACCOUNT' >&2; exit 2)
	$(UV) run --no-sync python -m adaptive_trader.cli resume --config $(PAPER_CONFIG) \
		--acknowledge I_HAVE_REVIEWED_THE_PAPER_ACCOUNT

report:
	$(UV) run --no-sync python -m adaptive_trader.cli report --config $(OBSERVER_CONFIG)

dashboard:
	./scripts/run_dashboard.sh

backup-database:
	./scripts/backup_database.sh

docker-build:
	docker compose build

offline-up:
	docker compose up --build

offline-down:
	docker compose down

docker-observe: offline-up

docker-paper:
	@echo "PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS"
	@echo "Tracked configuration remains submission-disabled; every paper gate still applies."
	docker compose --profile paper up paper-execution-worker dashboard

clean-generated:
	@test "$(CONFIRM)" = "I_ACKNOWLEDGE_DELETE_GENERATED_RESEARCH_OUTPUTS" || \
		(echo 'Refusing. Re-run with CONFIRM=I_ACKNOWLEDGE_DELETE_GENERATED_RESEARCH_OUTPUTS' >&2; exit 2)
	@if [ ! -d outputs ]; then \
		echo "No generated outputs to remove."; \
	else \
		find outputs -mindepth 1 -maxdepth 1 -type d \
			\( -name 'backtest*' -o -name 'replay*' -o -name 'dashboard_*' \
			-o -name 'historical_backtest' -o -name 'primary_forward_paper' \
			-o -name 'default_run' -o -name 'smoke_run*' \) -exec rm -rf -- {} +; \
	fi
