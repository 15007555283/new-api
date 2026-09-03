<<<<<<< HEAD
FRONTEND_DIR = ./web/default
BACKEND_DIR = .
DEV_FRONTEND_DEFAULT_PORT ?= 5173
DEV_FRONTEND_CLASSIC_PORT ?= 5174
=======
WEB_DIR = ./web
API_DIR = .
DEV_WEB_PORT ?= 5173
>>>>>>> v1.0.0-rc.30
DEV_COMPOSE_FILE = docker-compose.dev.yml
DEV_POSTGRES_SERVICE = postgres
DEV_API_SERVICE = new-api
DEV_POSTGRES_DB = new-api
DEV_POSTGRES_USER = root
DEV_SQLITE_PATH ?= one-api.db
BUILD_FILE_DIR = ./dist
REGISTER_URL := registry.cn-shenzhen.aliyuncs.com
NAME_SPACE := iootx_ai

<<<<<<< HEAD
.PHONY: all build-frontend build-all-frontends build-backend build-no-frontend start-backend dev dev-api dev-api-rebuild dev-web dev-web-classic reset-setup
=======
.PHONY: all build-web build-all-web start-api dev dev-api dev-api-rebuild dev-web reset-setup test
>>>>>>> v1.0.0-rc.30

all: build-all-web start-api

<<<<<<< HEAD
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
=======
build-web:
	@echo "Building web frontend..."
	@cd $(WEB_DIR) && bun install --frozen-lockfile
	@cd $(WEB_DIR) && DISABLE_ESLINT_PLUGIN='true' VITE_REACT_APP_VERSION=$$(cat ../VERSION) bun run build

build-all-web: build-web

start-api:
	@echo "Starting api dev server..."
	@cd $(API_DIR) && go run main.go &
>>>>>>> v1.0.0-rc.30

dev-api:
	@echo "Starting api services (docker)..."
	@docker compose -f $(DEV_COMPOSE_FILE) up -d

dev-api-rebuild:
	@echo "Rebuilding and starting api service (docker)..."
	@docker compose -f $(DEV_COMPOSE_FILE) up -d --build $(DEV_API_SERVICE)

dev-web:
	@echo "Starting web frontend dev server..."
	@echo "Web frontend: http://localhost:$(DEV_WEB_PORT)"
	@cd $(WEB_DIR) && bun install
	@cd $(WEB_DIR) && bun run dev -- --host 0.0.0.0 --port $(DEV_WEB_PORT)

dev: dev-api dev-web

# The main package embeds the ignored web/dist output and is covered after build-web.
test:
	@echo "Testing root Go module..."
	@root_module=$$(GOWORK=off go list -m); \
		root_packages=$$(GOWORK=off go list -e ./... | grep -vxF "$$root_module"); \
		GOWORK=off go test $$root_packages
	@echo "Testing relaykit Go module..."
	@cd relaykit && GOWORK=off go test ./...

reset-setup:
	@echo "Resetting local setup wizard state..."
	@if docker compose -f $(DEV_COMPOSE_FILE) ps --services --status running | grep -qx "$(DEV_POSTGRES_SERVICE)"; then \
		echo "Detected running docker dev PostgreSQL. Removing setup record and root users..."; \
		docker compose -f $(DEV_COMPOSE_FILE) exec -T $(DEV_POSTGRES_SERVICE) \
			psql -U $(DEV_POSTGRES_USER) -d $(DEV_POSTGRES_DB) \
			-c 'DELETE FROM setups;' \
			-c 'DELETE FROM users WHERE role = 100;' \
			-c "DELETE FROM options WHERE key IN ('SelfUseModeEnabled', 'DemoSiteEnabled');"; \
		echo "Restarting docker dev api so setup status is recalculated..."; \
		docker compose -f $(DEV_COMPOSE_FILE) restart $(DEV_API_SERVICE); \
	elif db_path="$${SQLITE_PATH:-$(DEV_SQLITE_PATH)}"; db_path="$${db_path%%\?*}"; [ -f "$$db_path" ]; then \
		db_path="$${SQLITE_PATH:-$(DEV_SQLITE_PATH)}"; \
		db_path="$${db_path%%\?*}"; \
		echo "Detected local SQLite database: $$db_path"; \
		sqlite3 "$$db_path" \
			"DELETE FROM setups; DELETE FROM users WHERE role = 100; DELETE FROM options WHERE key IN ('SelfUseModeEnabled', 'DemoSiteEnabled');"; \
		echo "SQLite setup state reset. Restart the local api process before testing the setup wizard."; \
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
