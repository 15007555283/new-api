FRONTEND_DIR = ./web/default
BACKEND_DIR = .
DEV_FRONTEND_DEFAULT_PORT ?= 5173
DEV_FRONTEND_CLASSIC_PORT ?= 5174
DEV_COMPOSE_FILE = docker-compose.dev.yml
DEV_POSTGRES_SERVICE = postgres
DEV_BACKEND_SERVICE = new-api
DEV_POSTGRES_DB = new-api
DEV_POSTGRES_USER = root
DEV_SQLITE_PATH ?= one-api.db
BUILD_FILE_DIR = ./dist
REGISTER_URL := registry.cn-shenzhen.aliyuncs.com
NAME_SPACE := iootx_ai

.PHONY: all build-frontend build-all-frontends build-backend build-no-frontend start-backend dev dev-api dev-api-rebuild dev-web dev-web-classic reset-setup

all: build-all-frontends start-backend

build-backend:
	@echo "Building backend only (no frontend)..."
	@cd $(BACKEND_DIR) && go build -tags no_frontend -o new-api

build-admin:
	@echo "Building backend only (no frontend)..."
	@cd $(BACKEND_DIR) && go build -o new-api-admin

build-no-frontend: build-backend

build-frontend:
	@echo "Building default frontend..."
	@cd ./web && bun install --frozen-lockfile
	@cd $(FRONTEND_DIR) && DISABLE_ESLINT_PLUGIN='true' VITE_REACT_APP_VERSION=$(cat ../../VERSION) bun run build

build-all-frontends: build-frontend

start-backend:
	@echo "Starting backend dev server..."
	@cd $(BACKEND_DIR) && go run . &

dev-api:
	@echo "Starting backend services (docker)..."
	@docker compose -f $(DEV_COMPOSE_FILE) up -d

dev-api-rebuild:
	@echo "Rebuilding and starting backend service (docker)..."
	@docker compose -f $(DEV_COMPOSE_FILE) up -d --build $(DEV_BACKEND_SERVICE)

dev-web:
	@echo "Starting both frontend dev servers..."
	@echo "Default frontend: http://localhost:$(DEV_FRONTEND_DEFAULT_PORT)"
	@echo "Classic frontend: http://localhost:$(DEV_FRONTEND_CLASSIC_PORT)"
	@cd ./web && bun install
	@(cd $(FRONTEND_DIR) && bun run dev -- --host 0.0.0.0 --port $(DEV_FRONTEND_DEFAULT_PORT)) & \
		default_pid=$$!; \
		(cd $(FRONTEND_CLASSIC_DIR) && bun run dev -- --host 0.0.0.0 --port $(DEV_FRONTEND_CLASSIC_PORT)) & \
		classic_pid=$$!; \
		trap 'kill $$default_pid $$classic_pid 2>/dev/null; wait $$default_pid $$classic_pid 2>/dev/null; exit 130' INT TERM; \
		while kill -0 $$default_pid 2>/dev/null && kill -0 $$classic_pid 2>/dev/null; do \
			sleep 1; \
		done; \
		if ! kill -0 $$default_pid 2>/dev/null; then \
			wait $$default_pid; \
			status=$$?; \
			kill $$classic_pid 2>/dev/null; \
			wait $$classic_pid 2>/dev/null; \
			exit $$status; \
		fi; \
		wait $$classic_pid; \
		status=$$?; \
		kill $$default_pid 2>/dev/null; \
		wait $$default_pid 2>/dev/null; \
		exit $$status

dev-web-classic:
	@echo "Starting classic frontend dev server..."
	@cd ./web && bun install
	@cd $(FRONTEND_CLASSIC_DIR) && bun run dev -- --host 0.0.0.0 --port $(DEV_FRONTEND_CLASSIC_PORT)

dev: dev-api dev-web

reset-setup:
	@echo "Resetting local setup wizard state..."
	@if docker compose -f $(DEV_COMPOSE_FILE) ps --services --status running | grep -qx "$(DEV_POSTGRES_SERVICE)"; then \
		echo "Detected running docker dev PostgreSQL. Removing setup record and root users..."; \
		docker compose -f $(DEV_COMPOSE_FILE) exec -T $(DEV_POSTGRES_SERVICE) \
			psql -U $(DEV_POSTGRES_USER) -d $(DEV_POSTGRES_DB) \
			-c 'DELETE FROM setups;' \
			-c 'DELETE FROM users WHERE role = 100;' \
			-c "DELETE FROM options WHERE key IN ('SelfUseModeEnabled', 'DemoSiteEnabled');"; \
		echo "Restarting docker dev backend so setup status is recalculated..."; \
		docker compose -f $(DEV_COMPOSE_FILE) restart $(DEV_BACKEND_SERVICE); \
	elif db_path="$${SQLITE_PATH:-$(DEV_SQLITE_PATH)}"; db_path="$${db_path%%\?*}"; [ -f "$$db_path" ]; then \
		db_path="$${SQLITE_PATH:-$(DEV_SQLITE_PATH)}"; \
		db_path="$${db_path%%\?*}"; \
		echo "Detected local SQLite database: $$db_path"; \
		sqlite3 "$$db_path" \
			"DELETE FROM setups; DELETE FROM users WHERE role = 100; DELETE FROM options WHERE key IN ('SelfUseModeEnabled', 'DemoSiteEnabled');"; \
		echo "SQLite setup state reset. Restart the local backend process before testing the setup wizard."; \
	else \
		echo "No running docker dev PostgreSQL or local SQLite database found."; \
		echo "Start the dev stack with 'make dev-api', or set SQLITE_PATH/DEV_SQLITE_PATH to your local SQLite database."; \
		exit 1; \
	fi

build-api-pro:
	@BUILD_MODE=pro; \
	IMAGE_VERSION=$$(./image.sh api_pro --read); \
	PROJECT_NAME=iootx_ai_api; \
	BUILD_FILE_NAME=$(BUILD_FILE_DIR)/$${PROJECT_NAME}-$$BUILD_MODE.$$IMAGE_VERSION; \
	BUILD_NAME=$(NAME_SPACE)/$${PROJECT_NAME}_$$BUILD_MODE; \
	echo "只跑后端编译（不包含前端）..."; \
	mkdir -p $(BUILD_FILE_DIR); \
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -tags no_frontend -o $$BUILD_FILE_NAME . && \
	echo "build prod linux OK" && \
	echo "FILE_NAME: $$BUILD_FILE_NAME" && \
	echo "BUILD_NAME: $$BUILD_NAME" && \
	echo "IMAGE_VERSION: $$IMAGE_VERSION" && \
	docker build --platform linux/amd64 -t $$BUILD_NAME:$$IMAGE_VERSION \
		--build-arg MODE=$$BUILD_MODE \
		--build-arg VERSION=$$IMAGE_VERSION \
		--build-arg PROJECT_NAME=$$PROJECT_NAME \
		-f ./Dockerfile.app . && \
	docker tag $$BUILD_NAME:$$IMAGE_VERSION $(REGISTER_URL)/$$BUILD_NAME:$$IMAGE_VERSION && \
	docker tag $$BUILD_NAME:$$IMAGE_VERSION $(REGISTER_URL)/$$BUILD_NAME:latest && \
	docker push $(REGISTER_URL)/$$BUILD_NAME:$$IMAGE_VERSION && \
	docker push $(REGISTER_URL)/$$BUILD_NAME:latest && \
	docker rmi $$BUILD_NAME:$$IMAGE_VERSION; \
	docker rmi $(REGISTER_URL)/$$BUILD_NAME:$$IMAGE_VERSION; \
	docker rmi $(REGISTER_URL)/$$BUILD_NAME:latest; \
	./image.sh api_pro --commit $$IMAGE_VERSION && \
	echo "push Execution completed. 🔚 "

build-admin-pro:
	@BUILD_MODE=pro; \
	PROJECT_NAME=iootx_ai_admin; \
	IMAGE_VERSION=$$(./image.sh admin_pro --read); \
	BUILD_FILE_NAME=$(BUILD_FILE_DIR)/$${PROJECT_NAME}-$$BUILD_MODE.$$IMAGE_VERSION; \
	BUILD_NAME=$(NAME_SPACE)/$${PROJECT_NAME}_$$BUILD_MODE; \
	echo "管理后台编译（包含前端页面）..."; \
	mkdir -p $(BUILD_FILE_DIR); \
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o $$BUILD_FILE_NAME . && \
	echo "build prod linux OK" && \
	echo "FILE_NAME: $$BUILD_FILE_NAME" && \
	echo "BUILD_NAME: $$BUILD_NAME" && \
	echo "IMAGE_VERSION: $$IMAGE_VERSION" && \
	docker build --platform linux/amd64 -t $$BUILD_NAME:$$IMAGE_VERSION \
		--build-arg MODE=$$BUILD_MODE \
		--build-arg VERSION=$$IMAGE_VERSION \
		--build-arg PROJECT_NAME=$$PROJECT_NAME \
		-f ./Dockerfile.app . && \
	docker tag $$BUILD_NAME:$$IMAGE_VERSION $(REGISTER_URL)/$$BUILD_NAME:$$IMAGE_VERSION && \
	docker tag $$BUILD_NAME:$$IMAGE_VERSION $(REGISTER_URL)/$$BUILD_NAME:latest && \
	docker push $(REGISTER_URL)/$$BUILD_NAME:$$IMAGE_VERSION && \
	docker push $(REGISTER_URL)/$$BUILD_NAME:latest && \
	docker rmi $$BUILD_NAME:$$IMAGE_VERSION; \
	docker rmi $(REGISTER_URL)/$$BUILD_NAME:$$IMAGE_VERSION; \
	docker rmi $(REGISTER_URL)/$$BUILD_NAME:latest; \
	./image.sh admin_pro --commit $$IMAGE_VERSION; \
	echo "push Execution completed. 🔚 "
