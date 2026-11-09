#!/usr/bin/env bash
# Configure NetBox for the netbox-windows-dhcp WinRM (read-only) transport in one command.
#
#   sudo bash contrib/apply-config.sh                      # NetBox in /opt/netbox, asks for account + password
#   sudo bash contrib/apply-config.sh /opt/netbox-4.6.9
#   sudo bash contrib/apply-config.sh /opt/netbox --username 'CORP\svc-netbox-dhcp' [--auth ntlm]
#
# What it does:
#   1. Password -> /etc/netbox/dhcp-winrm.env (root:root 0600), base64-encoded so any character works.
#      It is NEVER written to configuration.py.
#   2. systemd drop-ins so netbox and netbox-rq load that file (EnvironmentFile=).
#   3. A marked block in configuration.py: enables the plugin, transport = winrm, the account name,
#      and reads the password from the environment. Nothing else in the file is touched.
#   4. Backs up everything, runs "manage.py check", and restores ALL changes if the check fails.
# Re-running replaces the block and the password (e.g. after a password change); nothing is duplicated.
# It does not run migrations or restart services; it prints those commands.
set -euo pipefail

NB=/opt/netbox
WINRM_USER=''
AUTH=ntlm
SERVICES=${NBDHCP_SERVICES:-"netbox netbox-rq"}
ENV_FILE=${NBDHCP_ENV_FILE:-/etc/netbox/dhcp-winrm.env}
SYSTEMD_DIR=${NBDHCP_SYSTEMD_DIR:-/etc/systemd/system}
DROPIN_NAME=netbox-windows-dhcp.conf
BEGIN='# >>> netbox-windows-dhcp managed block'
END='# <<< netbox-windows-dhcp managed block <<<'

while [ $# -gt 0 ]; do
    case "$1" in
        --username) WINRM_USER="$2"; shift 2 ;;
        --auth)     AUTH="$2"; shift 2 ;;
        -h|--help)  sed -n '2,17p' "$0"; exit 0 ;;
        -*)         echo "Unknown option $1"; exit 1 ;;
        *)          NB="$1"; shift ;;
    esac
done

CFG="$NB/netbox/netbox/configuration.py"
PY="$NB/venv/bin/python"
[ "$(id -u)" -eq 0 ] || { echo "ERROR: run with sudo"; exit 1; }
[ -f "$CFG" ] || { echo "ERROR: $CFG not found. Pass the NetBox path, e.g. /opt/netbox-4.6.9"; exit 1; }
[ -x "$PY" ]  || { echo "ERROR: $PY not found"; exit 1; }
case "$AUTH" in ntlm|negotiate|kerberos) ;; *) echo "ERROR: --auth must be ntlm, negotiate or kerberos"; exit 1 ;; esac
"$PY" -c 'import importlib.util as u, sys; sys.exit(0 if u.find_spec("netbox_windows_dhcp") and u.find_spec("pypsrp") else 1)' || {
    echo "ERROR: the plugin (and pypsrp) is not installed in $NB/venv. Run pip install first."; exit 1; }

# ------------------------------------------------------------------ inputs
if [ -z "$WINRM_USER" ]; then
    read -r -p 'WinRM account (e.g. CORP\svc-netbox-dhcp): ' WINRM_USER
fi
[ -n "$WINRM_USER" ] || { echo "ERROR: account is required"; exit 1; }
if [ -n "${NETBOX_DHCP_WINRM_PASSWORD:-}" ]; then
    PASS="$NETBOX_DHCP_WINRM_PASSWORD"
else
    read -r -s -p "Password for $WINRM_USER: " PASS; echo
    read -r -s -p 'Repeat password: ' PASS2; echo
    [ "$PASS" = "$PASS2" ] || { echo "ERROR: passwords do not match"; exit 1; }
fi
[ -n "$PASS" ] || { echo "ERROR: password is required"; exit 1; }
PASS_B64=$(printf '%s' "$PASS" | base64 -w0)
unset PASS PASS2
USER_LITERAL=$("$PY" -c 'import sys; print(repr(sys.argv[1]))' "$WINRM_USER")

# ------------------------------------------------------------------ backups (for rollback)
STAMP=$(date +%Y%m%d-%H%M%S)
BAK_CFG="$CFG.bak.$STAMP"
cp -p "$CFG" "$BAK_CFG"
BAK_ENV=''
if [ -f "$ENV_FILE" ]; then BAK_ENV="$ENV_FILE.bak.$STAMP"; cp -p "$ENV_FILE" "$BAK_ENV"; fi
NEW_DROPINS=()

rollback() {
    cp -p "$BAK_CFG" "$CFG"
    if [ -n "$BAK_ENV" ]; then cp -p "$BAK_ENV" "$ENV_FILE"; else rm -f "$ENV_FILE"; fi
    for f in "${NEW_DROPINS[@]:-}"; do [ -n "$f" ] && rm -f "$f"; done
    if [ -z "${NBDHCP_NO_SYSTEMCTL:-}" ] && command -v systemctl >/dev/null 2>&1; then systemctl daemon-reload 2>/dev/null || true; fi
    echo "ROLLED BACK: configuration.py, $ENV_FILE and the systemd drop-ins are as before."
}
# Any unexpected failure from here on undoes everything.
trap 'rc=$?; echo "FAILED (line $LINENO, exit $rc)"; trap - ERR; rollback; exit $rc' ERR

# ------------------------------------------------------------------ 1. secret file
install -d -m 0750 -o root -g root "$(dirname "$ENV_FILE")"
umask 077
printf '# netbox-windows-dhcp WinRM password (base64). Managed by contrib/apply-config.sh.\nNETBOX_DHCP_WINRM_PASSWORD_B64=%s\n' "$PASS_B64" > "$ENV_FILE"
chown root:root "$ENV_FILE"; chmod 0600 "$ENV_FILE"
echo "Password stored in $ENV_FILE (root only)"

# ------------------------------------------------------------------ 2. systemd drop-ins
for svc in $SERVICES; do
    dir="$SYSTEMD_DIR/$svc.service.d"
    f="$dir/$DROPIN_NAME"
    [ -f "$f" ] || NEW_DROPINS+=("$f")
    install -d -m 0755 "$dir"
    printf '[Service]\nEnvironmentFile=%s\n' "$ENV_FILE" > "$f"
    chmod 0644 "$f"
done
if [ -z "${NBDHCP_NO_SYSTEMCTL:-}" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
    for svc in $SERVICES; do
        systemctl cat "$svc.service" >/dev/null 2>&1 || echo "WARNING: service $svc not found; drop-in written anyway"
    done
fi
echo "systemd: $SERVICES load $ENV_FILE"

# ------------------------------------------------------------------ 3. configuration.py block
sed -i "\|^$BEGIN|,\|^$END|d" "$CFG"
cat >> "$CFG" <<PYEOF

$BEGIN (generated by contrib/apply-config.sh; re-run the script instead of editing) >>>
import base64 as _nbdhcp_b64
import os as _nbdhcp_os
PLUGINS = list(globals().get('PLUGINS', []))
if 'netbox_windows_dhcp' not in PLUGINS:
    PLUGINS.append('netbox_windows_dhcp')
PLUGINS_CONFIG = dict(globals().get('PLUGINS_CONFIG', {}))
_nbdhcp_cfg = dict(PLUGINS_CONFIG.get('netbox_windows_dhcp', {}))
_nbdhcp_cfg['transport'] = 'winrm'           # read-only: nothing is written to the DHCP servers
_nbdhcp_cfg['winrm'] = dict(_nbdhcp_cfg.get('winrm', {}), **{
    'username': $USER_LITERAL,
    # From $ENV_FILE via systemd; never stored in this file.
    'password': _nbdhcp_b64.b64decode(_nbdhcp_os.environ.get('NETBOX_DHCP_WINRM_PASSWORD_B64', '')).decode('utf-8'),
    'auth': '$AUTH',
})
PLUGINS_CONFIG['netbox_windows_dhcp'] = _nbdhcp_cfg
$END
PYEOF

# ------------------------------------------------------------------ 4. verify (with the env the services will see)
CHECK=''
if ! CHECK=$(cd "$NB/netbox" && env NETBOX_DHCP_WINRM_PASSWORD_B64="$PASS_B64" "$PY" manage.py check 2>&1); then
    trap - ERR
    echo "FAILED: manage.py check"
    echo "$CHECK" | tail -8
    rollback
    exit 1
fi
VERIFY=$(cd "$NB/netbox" && env NETBOX_DHCP_WINRM_PASSWORD_B64="$PASS_B64" "$PY" - <<'PYCHK' 2>&1
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')
django.setup()
from django.conf import settings
from netbox_windows_dhcp.transport import is_read_only_transport, transport_name
cfg = settings.PLUGINS_CONFIG['netbox_windows_dhcp']
assert 'netbox_windows_dhcp' in settings.PLUGINS, 'plugin not enabled'
assert transport_name() == 'winrm' and is_read_only_transport(), 'transport is not winrm'
assert cfg['winrm']['password'], 'password not visible to NetBox'
print(f"OK: plugin enabled, transport={transport_name()} (read-only), account={cfg['winrm']['username']}, auth={cfg['winrm']['auth']}")
PYCHK
) || { trap - ERR; echo "FAILED: $VERIFY" | tail -5; rollback; exit 1; }
trap - ERR
unset PASS_B64
echo "$VERIFY"
rm -f "$BAK_ENV"
echo "configuration.py backup: $BAK_CFG"
echo
echo "Next:"
echo "  sudo $PY $NB/netbox/manage.py migrate"
echo "  sudo $PY $NB/netbox/manage.py collectstatic --no-input"
echo "  sudo systemctl restart $SERVICES"
echo "Optional, before adding servers in the UI:"
echo "  sudo bash $(cd "$(dirname "$0")" && pwd)/test-winrm.sh $NB <dhcp-server-fqdn> [ca-cert.cer]"
