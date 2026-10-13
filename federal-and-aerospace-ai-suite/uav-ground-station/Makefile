# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

.PHONY: help init up up-vss down down-vss clean-vss-index delete-vss-volumes logs

ifneq (,$(wildcard .env))
  include .env
  export
endif

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / \
	  {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

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

up: init ## Build and start the ground-station viewer + mediamtx relay sidecar (H.264 Relay mode; requires remote pipelines started with --h264)
	docker compose -f docker-compose.yml -f docker-compose-mediamtx.yml up -d --build

down: ## Stop and remove the ground-station viewer + mediamtx relay sidecar
	docker compose -f docker-compose.yml -f docker-compose-mediamtx.yml down -v

VSS_COMPOSE = docker compose -f docker-compose.yml -f docker-compose-mediamtx.yml -f docker-compose-vss.yml

up-vss: init ## Relay mode + VSS search containers (enables Live Natural Language Search)
	$(VSS_COMPOSE) up -d --build

down-vss: ## Stop the relay + VSS stack, keeping indexed footage/embeddings
	$(VSS_COMPOSE) down

# Index, footage and their metadata must be cleared together to stay consistent;
# the downloaded CLIP/YOLOX models (vss-ov-models, vss-yolox-models) are kept.
VSS_INDEX_VOLUMES = vss-vdms-db vss-minio-data vss-pg-data vss-data-prep

clean-vss-index: ## Stop the relay + VSS stack and DELETE indexed footage/embeddings, keeping models
	$(VSS_COMPOSE) down
	@project=$$($(VSS_COMPOSE) config | awk '/^name:/{print $$2; exit}'); \
	for v in $(VSS_INDEX_VOLUMES); do \
		docker volume rm -f "$${project}_$$v" >/dev/null && echo "removed $${project}_$$v"; \
	done

delete-vss-volumes: ## DELETE ALL VSS volumes incl. downloaded models (asks first; FORCE=1 skips)
	@if [ "$(FORCE)" != "1" ]; then \
		printf "This deletes ALL VSS volumes, including ~1.6 GB of downloaded models. Continue? [y/N] "; \
		read ans; case "$$ans" in [yY]|[yY][eE][sS]) ;; *) echo "Aborted."; exit 1;; esac; \
	fi
	$(VSS_COMPOSE) down -v

logs: ## Tail logs from all running containers
	docker compose -f docker-compose.yml -f docker-compose-mediamtx.yml logs -f
