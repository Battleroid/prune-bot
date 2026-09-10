.PHONY: venv test lint fmt run build up down logs shell deploy

VENV := .venv/Scripts/python.exe

venv:
	uv venv --python 3.12 && uv pip install -e ".[dev]"

test:
	$(VENV) -m pytest -q

lint:
	$(VENV) -m ruff check src tests

fmt:
	$(VENV) -m ruff check --fix src tests

run:
	$(VENV) -m prunebot

build:
	docker compose build

up:
	docker compose up -d --build --force-recreate

down:
	docker compose down

logs:
	docker compose logs -f prune-bot

# Sync the working tree to spooky and rebuild there.
deploy:
	rsync -av --delete \
	  --exclude .git --exclude .venv --exclude data --exclude .env \
	  --exclude config.toml --exclude __pycache__ --exclude '*.egg-info' \
	  --exclude .pytest_cache --exclude .ruff_cache \
	  ./ spooky:~/git/discord-prune-bot/
	ssh spooky 'cd ~/git/discord-prune-bot && docker compose up -d --build --force-recreate'
