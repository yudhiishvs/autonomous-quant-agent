PYTHON ?= python3
PAPER_CONFIG ?= configs/paper.yaml
OBSERVER_CONFIG ?= configs/observer.yaml
BACKTEST_CONFIG ?= configs/backtest.yaml
REPLAY_CONFIG ?= configs/replay.yaml

.DEFAULT_GOAL := help

.PHONY: help install format lint typecheck test coverage check-paper-environment doctor backtest replay observe \
	paper-dry-run paper-run status reconcile halt resume report dashboard backup-database \
	docker-build docker-observe docker-paper clean-generated

help:
	@echo "PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS"
	@echo "Adaptive Portfolio Agent — safe operational targets"
	@echo "  install format lint typecheck test coverage check-paper-environment"
	@echo "  doctor backtest replay observe paper-dry-run"
	@echo "  status reconcile halt resume report dashboard backup-database"
	@echo "  docker-build docker-observe docker-paper clean-generated"
	@echo "Default behavior never submits an order. paper-run/docker-paper remain multi-gated."

install:
	$(PYTHON) -m pip install -e ".[dev,dashboard]"

format:
	$(PYTHON) -m ruff format .

lint:
	$(PYTHON) -m ruff format --check .
	$(PYTHON) -m ruff check .

typecheck:
	$(PYTHON) -m mypy src

test:
	$(PYTHON) -m pytest -q

coverage:
	$(PYTHON) -m pytest --cov=adaptive_trader --cov-report=term-missing --cov-report=html

check-paper-environment:
	./scripts/check_local_paper_environment.sh $(OBSERVER_CONFIG)

doctor:
	$(PYTHON) -m adaptive_trader.cli doctor --config $(OBSERVER_CONFIG)

backtest:
	$(PYTHON) -m adaptive_trader.cli backtest --config $(BACKTEST_CONFIG)

replay:
	$(PYTHON) -m adaptive_trader.cli replay --config $(REPLAY_CONFIG)

observe:
	./scripts/run_observer.sh $(OBSERVER_CONFIG)

paper-dry-run:
	$(PYTHON) -m adaptive_trader.cli paper-once --config $(OBSERVER_CONFIG) --dry-run

paper-run:
	./scripts/run_paper.sh $(PAPER_CONFIG)

status:
	$(PYTHON) -m adaptive_trader.cli status --config $(OBSERVER_CONFIG)

reconcile:
	$(PYTHON) -m adaptive_trader.cli reconcile --config $(OBSERVER_CONFIG)

halt:
	@test -n "$(REASON)" || (echo 'Usage: make halt REASON="operator reason"' >&2; exit 2)
	$(PYTHON) -m adaptive_trader.cli halt --config $(PAPER_CONFIG) --reason "$(REASON)"

resume:
	@test "$(ACK)" = "I_HAVE_REVIEWED_THE_PAPER_ACCOUNT" || \
		(echo 'Usage: make resume ACK=I_HAVE_REVIEWED_THE_PAPER_ACCOUNT' >&2; exit 2)
	$(PYTHON) -m adaptive_trader.cli resume --config $(PAPER_CONFIG) \
		--acknowledge I_HAVE_REVIEWED_THE_PAPER_ACCOUNT

report:
	$(PYTHON) -m adaptive_trader.cli report --config $(OBSERVER_CONFIG)

dashboard:
	./scripts/run_dashboard.sh

backup-database:
	./scripts/backup_database.sh

docker-build:
	docker compose build

docker-observe:
	docker compose up trader dashboard

docker-paper:
	@echo "PAPER TRADING — SIMULATED CAPITAL AND SIMULATED FILLS"
	@echo "Stop the observer trader before starting the paper profile. All CLI gates still apply."
	docker compose --profile paper up paper-trader dashboard

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
