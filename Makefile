.PHONY: help setup env auth-gmail run reset-db poll-inbox check

help: ## Show available commands
	@echo "pathway-rfp commands:"
	@grep -E '^[a-zA-Z_-]+:.*##' Makefile | awk 'BEGIN {FS = ":.*## "}; {printf "  %-14s %s\n", $$1, $$2}'

setup: ## Install dependencies via uv
	uv sync

env: ## Create .env from .env.example if missing
	@if [ -f .env ]; then \
		echo ".env already exists"; \
	else \
		cp .env.example .env && echo "Created .env from .env.example"; \
	fi

auth-gmail: ## Run Gmail OAuth flow and generate token.json
	uv run python reauth.py

run: ## Start Streamlit UI
	uv run streamlit run ui/streamlit_app.py

reset-db: ## Reset DB tables while preserving BLS cache
	uv run python reset_db.py

poll-inbox: ## Run inbox polling helper
	uv run python poll_inbox.py

check: ## Compile-check key modules
	uv run python -m py_compile ui/streamlit_app.py app/services/inbox_monitor.py app/services/email_sender.py
