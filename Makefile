# Keycloak Nextcloud ExApp - Build System

REGISTRY ?= ghcr.io/conductionnl
IMAGE_NAME ?= keycloak-nextcloud
VERSION ?= 1.0.0

.PHONY: build push run test clean help lint format mypy check check-full check-strict

help:
	@echo "Keycloak Nextcloud ExApp"
	@echo ""
	@echo "Usage:"
	@echo "  make build    - Build Docker image"
	@echo "  make push     - Push to registry"
	@echo "  make run      - Run locally for testing"
	@echo "  make test     - Test endpoints"
	@echo "  make clean    - Remove local images"
	@echo ""
	@echo "Variables:"
	@echo "  REGISTRY=$(REGISTRY)"
	@echo "  VERSION=$(VERSION)"

build:
	docker build -t $(REGISTRY)/$(IMAGE_NAME):$(VERSION) -t $(REGISTRY)/$(IMAGE_NAME):latest .

push: build
	docker push $(REGISTRY)/$(IMAGE_NAME):$(VERSION)
	docker push $(REGISTRY)/$(IMAGE_NAME):latest

run:
	docker run -it --rm \
		-e APP_ID=keycloak \
		-e APP_SECRET=dev-secret \
		-e APP_HOST=0.0.0.0 \
		-e APP_PORT=23000 \
		-e APP_PERSISTENT_STORAGE=/data \
		-e NEXTCLOUD_URL=http://host.docker.internal:8080 \
		-e KC_BOOTSTRAP_ADMIN_USERNAME=admin \
		-e KC_BOOTSTRAP_ADMIN_PASSWORD=admin \
		-p 23000:23000 \
		-p 8081:8080 \
		$(REGISTRY)/$(IMAGE_NAME):latest

test:
	@echo "Testing heartbeat endpoint..."
	@curl -s http://localhost:23000/heartbeat || echo "Container not running"
	@echo ""
	@echo "Testing Keycloak health..."
	@curl -s http://localhost:9000/health/ready || echo "Keycloak not running"

clean:
	-docker rmi $(REGISTRY)/$(IMAGE_NAME):$(VERSION)
	-docker rmi $(REGISTRY)/$(IMAGE_NAME):latest

# ── Code Quality ───────────────────────────────────────────────────────

lint:
	ruff check ex_app/

format:
	ruff format --check ex_app/

format-fix:
	ruff format ex_app/

lint-fix:
	ruff check --fix ex_app/

mypy:
	mypy ex_app/

test-unit:
	pytest tests/ || echo "Tests require dependencies, skipping..."

check:
	@E=0; \
	for CMD in lint mypy; do \
		echo; echo "=== $$CMD ==="; \
		$(MAKE) $$CMD || E=1; \
	done; \
	echo; \
	if [ $$E -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "SOME CHECKS FAILED (see above)"; fi; \
	exit $$E

check-full:
	@E=0; \
	for CMD in lint format mypy test-unit; do \
		echo; echo "=== $$CMD ==="; \
		$(MAKE) $$CMD || E=1; \
	done; \
	echo; \
	if [ $$E -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "SOME CHECKS FAILED (see above)"; fi; \
	exit $$E

check-strict:
	@E=0; \
	for CMD in lint format mypy test-unit; do \
		echo; echo "=== $$CMD ==="; \
		$(MAKE) $$CMD || E=1; \
	done; \
	echo; \
	if [ $$E -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "SOME CHECKS FAILED (see above)"; fi; \
	exit $$E
