# Makefile — Agnes on foundryai-development (GRPN hackathon deploy)
#
# This is a manual-deploy helper used while the full Terraform flow is
# blocked by GRPN org policies (iam.disableServiceAccountKeyCreation,
# no projectIamAdmin delegation). It targets the existing VM
# foundryai-development in prj-grp-foundryai-dev-7c37.
#
# Once WIF + Terraform is unblocked, this file moves to a private
# keboola/agnes-infra-grpn repo and most targets become obsolete.
#
# Usage:
#   make -C scripts/grpn help
#   make -C scripts/grpn deploy
#   make -C scripts/grpn status
#   make -C scripts/grpn tunnel

SHELL := /bin/bash

# -------- overridable config (safe defaults for GRPN foundryai-development) --------
PROJECT        ?= prj-grp-foundryai-dev-7c37
ZONE           ?= us-central1-a
VM             ?= foundryai-development
APP_DIR        ?= /opt/agnes
LOCAL_PORT     ?= 8000
VM_PORT        ?= 8000
IMAGE          ?= ghcr.io/keboola/agnes-the-ai-analyst
ADMIN_EMAIL    ?= e_zsrotyr@groupon.com

# compose files (note: host-mount overlay binds /data from host = boot-disk ephemeral for this VM)
COMPOSE_FILES = -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.host-mount.yml
COMPOSE       = sudo docker compose $(COMPOSE_FILES)
SSH           = gcloud compute ssh $(VM) --zone=$(ZONE) --project=$(PROJECT)
SCP           = gcloud compute scp --zone=$(ZONE) --project=$(PROJECT)

# -------- help --------
.PHONY: help
help:
	@echo "Agnes @ $(VM) (project: $(PROJECT), zone: $(ZONE))"
	@echo ""
	@echo "  make deploy          Pull latest :stable image, recreate containers (zero-downtime if healthy)"
	@echo "  make deploy-tag TAG=stable-2026.04.83   Pull a specific tag instead of floating :stable"
	@echo "  make status          Health + version endpoint"
	@echo "  make logs            Tail app logs (ctrl-c to exit)"
	@echo "  make logs-scheduler  Tail scheduler logs"
	@echo "  make restart         docker compose restart (keeps state)"
	@echo "  make stop            docker compose stop (containers down, volumes preserved)"
	@echo "  make start           docker compose up -d"
	@echo "  make recreate        docker compose down + up -d (fresh containers, same data)"
	@echo ""
	@echo "  make ssh             Open interactive SSH session to the VM"
	@echo "  make tunnel          Start IAP tunnel; open http://localhost:$(LOCAL_PORT) in browser"
	@echo "  make open            Start tunnel AND open browser (macOS only)"
	@echo ""
	@echo "  make bootstrap-admin PASSWORD=<new-pwd>   Create admin (first-time only; 403 once any user has password)"
	@echo "  make set-data-source SOURCE=bigquery      Edit .env DATA_SOURCE; restart app"
	@echo ""
	@echo "  make install-cron    Install auto-upgrade cron (pulls :stable every 5 min, restarts on digest change)"
	@echo "  make uninstall-cron  Remove auto-upgrade cron"
	@echo ""
	@echo "  make env             Show .env keys (values NOT printed)"
	@echo "  make version         What version/channel/commit is running now"
	@echo "  make ps              docker ps on the VM"

# -------- deployment --------
.PHONY: deploy deploy-tag recreate restart start stop
deploy:
	$(SSH) --command='cd $(APP_DIR) && $(COMPOSE) pull && $(COMPOSE) up -d'
	@$(MAKE) --no-print-directory status

deploy-tag:
	@test -n "$(TAG)" || (echo "Usage: make deploy-tag TAG=stable-2026.04.83" >&2; exit 2)
	$(SSH) --command='cd $(APP_DIR) && sudo sed -i "s|^AGNES_TAG=.*|AGNES_TAG=$(TAG)|" .env && $(COMPOSE) pull && $(COMPOSE) up -d'
	@$(MAKE) --no-print-directory status

recreate:
	$(SSH) --command='cd $(APP_DIR) && $(COMPOSE) down && $(COMPOSE) up -d'
	@$(MAKE) --no-print-directory status

restart:
	$(SSH) --command='cd $(APP_DIR) && $(COMPOSE) restart'

start:
	$(SSH) --command='cd $(APP_DIR) && $(COMPOSE) up -d'

stop:
	$(SSH) --command='cd $(APP_DIR) && $(COMPOSE) down'

# -------- observability --------
.PHONY: status version ps env logs logs-scheduler
status:
	@echo "=== health (via IAP tunnel on VM) ==="
	@$(SSH) --command='curl -sf --max-time 10 http://localhost:$(VM_PORT)/api/health' 2>&1 | tail -1 | python3 -m json.tool 2>/dev/null | head -10 || echo "not healthy"

version:
	@$(SSH) --command='curl -sf --max-time 10 http://localhost:$(VM_PORT)/api/version' 2>&1 | tail -1 | python3 -m json.tool 2>/dev/null | head -10 || echo "unreachable"

ps:
	$(SSH) --command='sudo docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"'

env:
	@echo "=== .env keys on VM (values not shown) ==="
	$(SSH) --command='sudo cut -d= -f1 $(APP_DIR)/.env'

logs:
	$(SSH) --command='sudo docker logs -f --tail 100 agnes-app-1'

logs-scheduler:
	$(SSH) --command='sudo docker logs -f --tail 100 agnes-scheduler-1'

# -------- access --------
.PHONY: ssh tunnel open
ssh:
	$(SSH)

tunnel:
	@echo "Starting IAP tunnel — http://localhost:$(LOCAL_PORT) is now Agnes"
	@echo "Leave this terminal open; Ctrl-C to stop."
	gcloud compute start-iap-tunnel $(VM) $(VM_PORT) \
		--local-host-port=localhost:$(LOCAL_PORT) \
		--zone=$(ZONE) --project=$(PROJECT)

open:
	@( gcloud compute start-iap-tunnel $(VM) $(VM_PORT) \
		--local-host-port=localhost:$(LOCAL_PORT) \
		--zone=$(ZONE) --project=$(PROJECT) & \
		sleep 4 && open "http://localhost:$(LOCAL_PORT)/login" && wait )

# -------- one-off operations --------
.PHONY: bootstrap-admin set-data-source install-cron uninstall-cron
bootstrap-admin:
	@test -n "$(PASSWORD)" || (echo "Usage: make bootstrap-admin PASSWORD=<secret>" >&2; exit 2)
	@$(SSH) --command='curl -sS -X POST http://localhost:$(VM_PORT)/auth/bootstrap \
		-H "Content-Type: application/json" \
		-d "{\"email\":\"$(ADMIN_EMAIL)\",\"password\":\"$(PASSWORD)\"}"' 2>&1 | tail -1 | python3 -m json.tool 2>/dev/null | head -8

set-data-source:
	@test -n "$(SOURCE)" || (echo "Usage: make set-data-source SOURCE=bigquery|csv|keboola" >&2; exit 2)
	$(SSH) --command='sudo sed -i "s|^DATA_SOURCE=.*|DATA_SOURCE=$(SOURCE)|" $(APP_DIR)/.env && cd $(APP_DIR) && $(COMPOSE) up -d --force-recreate app'
	@$(MAKE) --no-print-directory status

install-cron:
	$(SCP) agnes-auto-upgrade.sh $(VM):/tmp/agnes-auto-upgrade.sh
	$(SSH) --command='sudo install -m 755 /tmp/agnes-auto-upgrade.sh /usr/local/bin/agnes-auto-upgrade.sh && rm /tmp/agnes-auto-upgrade.sh && ( sudo crontab -l 2>/dev/null | grep -v agnes-auto-upgrade || true; echo "*/5 * * * * /usr/local/bin/agnes-auto-upgrade.sh >> /var/log/agnes-auto-upgrade.log 2>&1" ) | sudo crontab - && echo "cron installed"'

uninstall-cron:
	$(SSH) --command='( sudo crontab -l 2>/dev/null | grep -v agnes-auto-upgrade ) | sudo crontab - && sudo rm -f /usr/local/bin/agnes-auto-upgrade.sh && echo "cron removed"'
