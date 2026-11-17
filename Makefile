# Garmin personal dashboard. The project path contains spaces, so paths are quoted.
ROOT    := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
UV      := $(shell command -v uv)
LABEL   := com.garmin-dashboard.sync
PLIST   := $(HOME)/Library/LaunchAgents/$(LABEL).plist

.PHONY: install sync backfill dashboard mcp mcp-test test install-schedule uninstall-schedule run-schedule-now

install:            ## install Python 3.12 + deps
	uv sync

sync:               ## incremental sync (last synced date - 3 days -> today)
	uv run python -m garmin.sync

backfill:           ## walk back through history; optional SINCE=YYYY-MM-DD
	uv run python -m garmin.sync --backfill $(if $(SINCE),--since $(SINCE),)

dashboard:          ## open the Streamlit dashboard
	uv run streamlit run dashboard/app.py

mcp:                ## run the MCP server on stdio (what Claude Desktop launches)
	uv run python mcp_server/server.py

mcp-test:           ## open the MCP Inspector against the server
	npx -y @modelcontextprotocol/inspector uv --directory "$(ROOT)" run python mcp_server/server.py

test:
	uv run pytest -q

install-schedule:   ## daily 06:00 sync via launchd, logs in logs/
	@mkdir -p "$(ROOT)/logs" "$(HOME)/Library/LaunchAgents"
	@sed -e 's|__ROOT__|$(ROOT)|g' -e 's|__UV__|$(UV)|g' -e 's|__LABEL__|$(LABEL)|g' \
		launchd/sync.plist.template > "$(PLIST)"
	-@launchctl bootout gui/$$(id -u) "$(PLIST)" 2>/dev/null
	launchctl bootstrap gui/$$(id -u) "$(PLIST)"
	@echo "Installed $(PLIST) (runs daily at 06:00, logs to logs/sync.log)"

uninstall-schedule:
	-launchctl bootout gui/$$(id -u) "$(PLIST)"
	rm -f "$(PLIST)"

run-schedule-now:   ## trigger the launchd job immediately
	launchctl kickstart -k gui/$$(id -u)/$(LABEL)

# ---------------------------------------------------------------- always-on dashboard + Tailscale
DLABEL  := com.garmin-dashboard.web
DPLIST  := $(HOME)/Library/LaunchAgents/$(DLABEL).plist
# Tailscale CLI: on PATH (brew/standalone "Install CLI") or inside the app bundle
TS      := $(or $(shell command -v tailscale),/Applications/Tailscale.app/Contents/MacOS/Tailscale)

.PHONY: install-dashboard uninstall-dashboard restart-dashboard tailscale-serve tailscale-unserve remote-status

install-dashboard:  ## always-on dashboard on 127.0.0.1:8501 (starts at login, restarts on crash)
	@mkdir -p "$(ROOT)/logs" "$(HOME)/Library/LaunchAgents"
	@sed -e 's|__ROOT__|$(ROOT)|g' -e 's|__UV__|$(UV)|g' -e 's|__LABEL__|$(DLABEL)|g' \
		launchd/dashboard.plist.template > "$(DPLIST)"
	-@launchctl bootout gui/$$(id -u) "$(DPLIST)" 2>/dev/null
	launchctl bootstrap gui/$$(id -u) "$(DPLIST)"
	@echo "Installed $(DPLIST) -> http://127.0.0.1:8501 (logs/dashboard.log)"

uninstall-dashboard:
	-launchctl bootout gui/$$(id -u) "$(DPLIST)"
	rm -f "$(DPLIST)"

restart-dashboard:  ## pick up code changes
	launchctl kickstart -k gui/$$(id -u)/$(DLABEL)

tailscale-serve:    ## expose the dashboard to your tailnet only (HTTPS, never the public internet)
	"$(TS)" serve --bg --https=443 http://127.0.0.1:8501
	@"$(TS)" serve status

tailscale-unserve:
	"$(TS)" serve --https=443 off

remote-status:      ## where things stand
	@launchctl print gui/$$(id -u)/$(DLABEL) 2>/dev/null | grep -E "state =|pid =" || echo "dashboard service: not installed"
	@launchctl print gui/$$(id -u)/$(LABEL) 2>/dev/null | grep -E "state =" | sed 's/^/sync job: /' || echo "sync job: not installed"
	@curl -sf -o /dev/null http://127.0.0.1:8501/_stcore/health && echo "local health: ok" || echo "local health: DOWN"
	@"$(TS)" serve status 2>/dev/null || echo "tailscale: not running / not installed"
