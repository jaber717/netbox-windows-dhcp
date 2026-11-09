#!/usr/bin/env bash
# Read-only connectivity test from NetBox to one DHCP server's JEA endpoint, using the
# plugin's own WinRM client and the credentials configured by apply-config.sh.
#
#   sudo bash contrib/test-winrm.sh /opt/netbox dhcp01.corp.example [winrm-dhcp01.cer]
#
# Prints health, scope count, failover relationships and the lease count per scope.
# Writes nothing to NetBox or to the DHCP server.
set -euo pipefail
NB="${1:?usage: test-winrm.sh <netbox-path> <dhcp-fqdn> [ca-cert]}"
HOST="${2:?usage: test-winrm.sh <netbox-path> <dhcp-fqdn> [ca-cert]}"
CA="${3:-}"
ENV_FILE=${NBDHCP_ENV_FILE:-/etc/netbox/dhcp-winrm.env}
PY="$NB/venv/bin/python"
[ -x "$PY" ] || { echo "ERROR: $PY not found"; exit 1; }
[ -z "$CA" ] || [ -f "$CA" ] || { echo "ERROR: CA file $CA not found"; exit 1; }
[ -r "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not readable (run apply-config.sh first, with sudo)"; exit 1; }
set -a; . "$ENV_FILE"; set +a
cd "$NB/netbox"
NBDHCP_HOST="$HOST" NBDHCP_CA="$CA" "$PY" - <<'PYEOF'
import os, sys, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')
django.setup()
from types import SimpleNamespace
from netbox_windows_dhcp.api_client import PSUClientError
from netbox_windows_dhcp.winrm_client import WinRMClient

ca_path = os.environ.get('NBDHCP_CA') or ''
ca_pem = ''
if ca_path:
    raw = open(ca_path, 'rb').read()
    if b'BEGIN CERTIFICATE' in raw:
        ca_pem = raw.decode()
    else:  # DER .cer
        import base64, textwrap
        ca_pem = '-----BEGIN CERTIFICATE-----\n' + '\n'.join(textwrap.wrap(base64.b64encode(raw).decode(), 64)) + '\n-----END CERTIFICATE-----\n'
host = os.environ['NBDHCP_HOST']
server = SimpleNamespace(name=host, hostname=host, port=5986, use_https=True, verify_ssl=True,
                         ca_cert=ca_pem, ca_cert_expiry=None)
try:
    with WinRMClient(server) as c:
        print(f'Connecting to {host}:5986 as {c.username} ({c.auth}), JEA endpoint {c.configuration_name}')
        h = c.ping_read()
        print(f"  health      : {h.get('status')} (module version {h.get('version')})")
        scopes = c.list_scopes()
        print(f'  scopes      : {len(scopes)}')
        fos = c.list_failover()
        for f in fos:
            print(f"  failover    : {f.get('name')}  {f.get('primary_server')} <-> {f.get('secondary_server')}  "
                  f"mode={f.get('mode')}  scopes={len(f.get('scope_ids') or [])}")
        if not fos:
            print('  failover    : none')
        leases = c.list_leases()
        for s in scopes:
            print(f"    {s['scope_id']:<16} {s.get('name') or '':<28} {s.get('state'):<9} "
                  f"leases={len(leases.get(s['scope_id'], []))}")
        try:
            c.ping_write()
        except PSUClientError as e:
            print(f'  write access: refused (read-only) - status {e.status_code}')
    print('RESULT: OK')
except PSUClientError as e:
    print(f'RESULT: FAILED - {e}')
    sys.exit(1)
PYEOF
