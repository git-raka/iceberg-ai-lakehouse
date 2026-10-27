SHELL := /bin/bash
COMPOSE := docker compose
RUN := $(COMPOSE) run --rm -T tools
CLI := $(RUN) python -m lakehouse.cli
# Trino identity for `ask` and `sql` (not AS: make predefines that one)
ROLE ?= executive

.DEFAULT_GOAL := help

help: ## Show the available commands
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------------------------------- lifecycle

env: ## Create .env with random secrets (keeps an existing .env)
	@if [ -f .env ]; then echo ".env exists, leaving it alone"; else \
	  cp .env.example .env; \
	  for k in POSTGRES_PASSWORD S3_SECRET_KEY SUPERSET_SECRET_KEY SUPERSET_ADMIN_PASSWORD; do \
	    sed -i "s|^$$k=.*|$$k=$$(openssl rand -hex 16)|" .env; done; \
	  sed -i "s|^LAKEKEEPER_ENCRYPTION_KEY=.*|LAKEKEEPER_ENCRYPTION_KEY=$$(openssl rand -hex 24)|" .env; \
	  ka=admin-$$(openssl rand -hex 12); ke=exec-$$(openssl rand -hex 12); \
	  kf=finance-$$(openssl rand -hex 12); kt=t003-$$(openssl rand -hex 12); \
	  sed -i "s|^API_KEYS_JSON=.*|API_KEYS_JSON={\"$$ka\":\"admin\",\"$$ke\":\"executive\",\"$$kf\":\"analyst_finance\",\"$$kt\":\"tenant_t003\"}|" .env; \
	  sed -i "s|^MCP_API_KEY=.*|MCP_API_KEY=$$ke|" .env; \
	  echo ".env created with random secrets and API keys (Superset admin password is in .env)"; fi

up: env ## Build and start every service, then wait until they are healthy
	$(COMPOSE) --profile tools build
	$(COMPOSE) up -d
	$(COMPOSE) up -d --wait trino spark ollama superset

setup: up models seed pipeline superset ## Everything from zero: start, pull models, seed, run the pipeline, dashboards
	@echo
	@echo "Ready.  Dashboard  http://localhost:8088/superset/dashboard/executive/  (admin / password in .env)"
	@echo "        AI chat    http://localhost:8000/        Dagster  http://localhost:3001/"

status: ## Show the services and their health
	$(COMPOSE) ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'

down: ## Stop everything (data is kept)
	$(COMPOSE) --profile tools down

clean: ## Stop everything and delete all data volumes, including the downloaded models
	$(COMPOSE) --profile tools down -v

# ------------------------------------------------------------------------------------------- data

models: ## Pull the LLM and the embedding model into Ollama (about 5 GB, once)
	$(CLI) models

seed: ## Generate the synthetic sources (ERP, weighbridge, tickets, PDFs)
	$(CLI) seed

pipeline: ## Run the Dagster job: bronze -> silver -> AI -> gold -> embeddings, with quality checks
	$(RUN) dagster job execute -m lakehouse.orchestration.definitions -j lakehouse_pipeline > /dev/null 2>&1; \
	  status=$$?; $(CLI) run-summary; exit $$status

increment: ## Simulate one day of changes in the sources (then run `make pipeline`)
	$(CLI) increment

superset: ## Create or update the Superset database, datasets, charts and dashboard
	$(CLI) superset

# ------------------------------------------------------------------------------------------- demos

governance: ## Same queries as four identities: row filters and column masks
	$(CLI) governance

timetravel: ## Iceberg snapshots and CDC history of one tenant
	$(CLI) timetravel

ask: ## Ask the agent: make ask Q="Which leases expire next year?" [ROLE=tenant_t003]
	@test -n "$(Q)" || (echo 'usage: make ask Q="your question" [ROLE=executive|analyst_finance|tenant_t003|admin]'; exit 1)
	$(CLI) ask "$(Q)" --user $(ROLE)

eval: ## Score text-to-SQL against the evaluation set
	$(CLI) eval

sql: ## Open a Trino SQL shell: make sql [ROLE=tenant_t003]
	$(COMPOSE) exec trino trino --user $(ROLE) --catalog iceberg

# ------------------------------------------------------------------------------------------- development

test: ## Run the unit tests (no services needed)
	$(COMPOSE) run --rm -T --no-deps -v $(CURDIR)/tests:/app/tests tools python -m pytest -q

logs: ## Follow the logs of one service: make logs S=flink-taskmanager
	$(COMPOSE) logs -f --tail 100 $(S)

.PHONY: help env up setup status down clean models seed pipeline increment superset governance timetravel ask eval sql test logs
