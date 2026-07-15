#!/usr/bin/env bash
set -euo pipefail

case "${1:-status}" in
  status)
    tailscale serve status
    tailscale funnel status
    ;;
  apply)
    test "${KH_CONFIRM_TAILSCALE_SERVE:-}" = "server-ai-00:443-to-127.0.0.1:8092"
    tailscale funnel status | grep -Eiq 'off|no funnel|no serve config|not configured|available' || {
      echo 'Funnel may be enabled; refusing to configure Serve' >&2
      exit 1
    }
    tailscale serve --bg --https=443 localhost:8092
    tailscale serve status
    ;;
  rollback)
    tailscale serve --https=443 off
    tailscale serve status
    ;;
  *)
    echo 'usage: configure-serve.sh status|apply|rollback' >&2
    exit 2
    ;;
esac
