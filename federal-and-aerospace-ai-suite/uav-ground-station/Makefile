# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

.PHONY: help init up up-relay down down-relay logs

ifneq (,$(wildcard .env))
  include .env
  export
endif

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / \
	  {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

init: ## Create .env from template if missing, and auto-detect GPU/NPU device paths
	@if test -f .env; then \
		echo ".env already exists — skipping (delete it to re-run init)"; \
	else \
		cp .env.example .env; \
		echo "[init] .env ready. Edit VIZ_COMPANION_HOST to point at your UAV companion-compute board's IP."; \
		GPU_CARD=$$(ls -1 /dev/dri/card* 2>/dev/null | head -1); \
		GPU_RENDER=$$(ls -1 /dev/dri/renderD* 2>/dev/null | head -1); \
		if [ -n "$$GPU_CARD" ] && [ -n "$$GPU_RENDER" ]; then \
			echo "✅ GPU detected: GPU_DEVICE=$$GPU_CARD GPU_RENDER_DEVICE=$$GPU_RENDER"; \
			sed -i "s|^GPU_DEVICE=.*|GPU_DEVICE=$$GPU_CARD|" .env; \
			sed -i "s|^GPU_RENDER_DEVICE=.*|GPU_RENDER_DEVICE=$$GPU_RENDER|" .env; \
		else \
			echo "⚠  No Intel GPU found at /dev/dri/ — leaving GPU_DEVICE=/dev/null (gs-metrics-manager won't report GPU usage)."; \
		fi; \
		NPU_DEV=$$(ls -1 /dev/accel/accel* 2>/dev/null | head -1); \
		if [ -n "$$NPU_DEV" ]; then \
			echo "✅ NPU detected: NPU_DEVICE=$$NPU_DEV"; \
			sed -i "s|^NPU_DEVICE=.*|NPU_DEVICE=$$NPU_DEV|" .env; \
		else \
			echo "⚠  No Intel NPU found at /dev/accel/ — leaving NPU_DEVICE=/dev/null (gs-metrics-manager won't report NPU usage)."; \
		fi; \
	fi

up: init ## Build and start the ground-station viewer (WebRTC - Direct)
	docker compose up -d --build

up-relay: init ## Also start the mediamtx sidecar, for "H.264 Relay" mode (--h264 without --caption)
	docker compose -f docker-compose.yml -f docker-compose-mediamtx.yml up -d --build

down: ## Stop and remove the ground-station viewer
	docker compose down -v

down-relay: ## Stop and remove the ground-station viewer + mediamtx sidecar
	docker compose -f docker-compose.yml -f docker-compose-mediamtx.yml down -v

logs: ## Tail logs from all running containers
	docker compose logs -f
