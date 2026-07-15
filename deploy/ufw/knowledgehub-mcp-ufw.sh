#!/usr/bin/env bash
set -euo pipefail

ALLOW_COMMENT=KH-MCP-LAN-ALLOW
DENY_COMMENT=KH-MCP-LAN-DENY
LOOPBACK_COMMENT=KH-MCP-TAILSCALE-LOOPBACK

show_plan() {
  cat <<'EOF'
Planned rules (ordered):
  allow in on eno1 proto tcp from 10.249.43.193 to 10.249.44.27 port 8091
  deny  in on eno1 proto tcp from any          to 10.249.44.27 port 8091
  deny  in on eno1 proto tcp from any          to 10.249.44.27 port 8092
The script does not modify SSH or any other management rule.
EOF
  ufw status numbered
}

case "${1:-dry-run}" in
  dry-run)
    show_plan
    ;;
  backup)
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    install -d -m 0700 /var/backups/knowledgehub-mcp
    tar -C /etc -czf "/var/backups/knowledgehub-mcp/ufw-${stamp}.tar.gz" ufw
    echo "/var/backups/knowledgehub-mcp/ufw-${stamp}.tar.gz"
    ;;
  apply)
    test "${KH_CONFIRM_UFW_APPLY:-}" = '10.249.43.193-to-10.249.44.27:8091'
    ufw status | grep -Eq '22/tcp|OpenSSH|ssh' || {
      echo 'No obvious SSH management rule found; refusing to continue' >&2
      exit 1
    }
    "$0" backup
    ufw insert 1 allow in on eno1 proto tcp from 10.249.43.193 to 10.249.44.27 port 8091 comment "$ALLOW_COMMENT"
    ufw insert 2 deny in on eno1 proto tcp from any to 10.249.44.27 port 8091 comment "$DENY_COMMENT"
    ufw insert 3 deny in on eno1 proto tcp from any to 10.249.44.27 port 8092 comment "$LOOPBACK_COMMENT"
    "$0" verify
    ;;
  verify)
    ufw status numbered | grep -F "$ALLOW_COMMENT"
    ufw status numbered | grep -F "$DENY_COMMENT"
    ufw status numbered | grep -F "$LOOPBACK_COMMENT"
    ;;
  rollback)
    for comment in "$LOOPBACK_COMMENT" "$DENY_COMMENT" "$ALLOW_COMMENT"; do
      while number=$(ufw status numbered | sed -n "s/^\[ *\([0-9][0-9]*\)\].*${comment}.*/\1/p" | tail -1) && test -n "$number"; do
        ufw --force delete "$number"
      done
    done
    ufw status numbered
    ;;
  *)
    echo 'usage: knowledgehub-mcp-ufw.sh dry-run|backup|apply|verify|rollback' >&2
    exit 2
    ;;
esac
