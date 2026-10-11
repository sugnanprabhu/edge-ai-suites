# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

.PHONY: help init up up-relay down logs

ifneq (,$(wildcard .env))
  include .env
  export
endif

COMPOSE := docker-compose.yml
COMPOSE_RELAY := docker-compose.yml -f docker-compose-mediamtx.yml

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / \
	  {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

init: ## Create .env from template if missing
	@test -f .env || cp .env.example .env
	@echo "[init] .env ready. Edit VIZ_DEFAULT_HOST to point at your UAV stack's host IP."

up: init ## Build and start the ground-station viewer (WebRTC - Direct + MJPEG only)
	docker compose -f $(COMPOSE) up -d --build

up-relay: init ## Same as 'up', plus the legacy WebRTC - Relay fallback (mediamtx+ffmpeg sidecar)
	docker compose -f $(COMPOSE_RELAY) up -d --build

down: ## Stop and remove the ground-station viewer (includes volumes, both profiles)
	docker compose -f $(COMPOSE_RELAY) down -v

logs: ## Tail logs from all running containers
	docker compose -f $(COMPOSE_RELAY) logs -f
