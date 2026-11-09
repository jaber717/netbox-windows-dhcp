"""
WinRM / JEA transport for Windows DHCP - READ-ONLY. Used instead of PowerShell Universal when

    PLUGINS_CONFIG['netbox_windows_dhcp'] = {
        'transport': 'winrm',
        'winrm': {
            'username': 'CORP\\\\svc-netbox-dhcp',
            'password': os.environ['NETBOX_DHCP_WINRM_PASSWORD'],
            'auth': 'ntlm',                       # ntlm | negotiate | kerberos
            'configuration_name': 'NetboxDhcpReader',
        },
        # optional per-server credentials, keyed by the DHCP server's hostname in NetBox
        'server_overrides': {'dhcp02.corp.example': {'winrm_username': ..., 'winrm_password': ...}},
    }

Each call runs one Get-NbDhcp* function of the NetboxDhcpReader module through the JEA
endpoint on the DHCP server. Those functions are the plugin's own PSU GET endpoints, verbatim
(see tools/build_winrm_module.py), so the data has exactly the shape the plugin expects.

Server fields reused from the DHCPServer model: hostname, port (5986), use_https, verify_ssl,
ca_cert / ca_cert_expiry (PEM of the CA, or of the self-signed listener certificate).
The App Token field is not used.

Every write method refuses with PSUClientError(status_code=403). The plugin's health check
already maps a 403 from ping_write() to "Read-only" access.
"""
import json
import logging
import tempfile
from typing import Any, Dict, List, Optional

from .api_client import PSUClientError, _batch_error

logger = logging.getLogger('netbox_windows_dhcp')

DEFAULT_PORT = 5986
DEFAULT_CONFIGURATION = 'NetboxDhcpReader'
DEFAULT_AUTH = 'ntlm'
AUTH_METHODS = ('ntlm', 'negotiate', 'kerberos')
OPERATION_TIMEOUT = 60
READ_TIMEOUT = 90
CONNECTION_TIMEOUT = 30

READ_ONLY_MESSAGE = (
    'Read-only WinRM transport: {op} is not allowed. This NetBox is configured to read from the '
    'DHCP servers only (PLUGINS_CONFIG transport = winrm).'
)


def _winrm_settings(hostname: str) -> Dict[str, Any]:
    from .transport import plugin_config
    cfg = plugin_config()
    base = dict(cfg.get('winrm') or {})
    override = (cfg.get('server_overrides') or {}).get(hostname) or {}
    for key in ('username', 'password', 'auth', 'configuration_name'):
        if override.get(f'winrm_{key}') is not None:
            base[key] = override[f'winrm_{key}']
    return base


class WinRMClient:
    """Read-only DHCP client for one server, over WinRM HTTPS into the NetboxDhcpReader JEA endpoint."""

    transport = 'winrm'

    def __init__(self, server):
        self.server = server
        self._pool = None
        self._wsman = None
        self._cert_tempfile = None
        opts = _winrm_settings(server.hostname)
        self.username = opts.get('username') or ''
        self.password = opts.get('password') or ''
        self.auth = str(opts.get('auth') or DEFAULT_AUTH).lower()
        self.configuration_name = opts.get('configuration_name') or DEFAULT_CONFIGURATION
        self.operation_timeout = int(opts.get('operation_timeout') or OPERATION_TIMEOUT)
        self.read_timeout = max(int(opts.get('read_timeout') or READ_TIMEOUT), self.operation_timeout + 10)
        self.use_proxy = bool(opts.get('use_proxy', False))
        if self.auth not in AUTH_METHODS:
            raise PSUClientError(f"WinRM auth must be one of {', '.join(AUTH_METHODS)}, got {self.auth!r}")
        if not self.username or not self.password:
            raise PSUClientError(
                "WinRM credentials missing: set PLUGINS_CONFIG['netbox_windows_dhcp']['winrm'] "
                "username and password (or server_overrides winrm_username / winrm_password)."
            )
        self.port = server.port if server.port and server.port != 443 else DEFAULT_PORT
        self.cert_validation = self._cert_validation()

    # ------------------------------------------------------------------ connection

    def _cert_validation(self):
        if not self.server.use_https:
            return False
        if not self.server.verify_ssl:
            return False
        ca_cert = getattr(self.server, 'ca_cert', '') or ''
        if not ca_cert:
            return True
        expiry = getattr(self.server, 'ca_cert_expiry', None)
        if expiry:
            from django.utils import timezone
            if expiry < timezone.now():
                raise PSUClientError(
                    f'The stored CA certificate for "{self.server.name}" expired on {expiry.date()}. '
                    'Re-import it from the Server detail page in NetBox.'
                )
        self._cert_tempfile = tempfile.NamedTemporaryFile(suffix='.pem', mode='w', delete=True)
        self._cert_tempfile.write(ca_cert)
        self._cert_tempfile.flush()
        return self._cert_tempfile.name

    def _open(self):
        if self._pool is not None:
            return self._pool
        from pypsrp.powershell import RunspacePool
        from pypsrp.wsman import WSMan
        try:
            self._wsman = WSMan(
                self.server.hostname,
                port=self.port,
                ssl=bool(self.server.use_https),
                username=self.username,
                password=self.password,
                auth=self.auth,
                cert_validation=self.cert_validation,
                encryption='auto',
                # DHCP servers are internal: never send WinRM through the https_proxy the
                # NetBox host may have for GitHub/PyPI, unless explicitly asked to.
                no_proxy=not self.use_proxy,
                connection_timeout=CONNECTION_TIMEOUT,
                operation_timeout=self.operation_timeout,
                read_timeout=self.read_timeout,
            )
            pool = RunspacePool(self._wsman, configuration_name=self.configuration_name)
            pool.open()
        except Exception as exc:
            raise PSUClientError(self._connect_hint(exc)) from exc
        self._pool = pool
        return pool

    def _connect_hint(self, exc) -> str:
        text = f'WinRM connection to {self.server.hostname}:{self.port} failed: {exc}'
        low = str(exc).lower()
        if 'certificate' in low or 'ssl' in low:
            text += (' (check the server certificate: CN/SAN must match the hostname, and the CA certificate '
                     'in NetBox must be the issuer or the self-signed certificate)')
        elif '401' in low or 'unauthor' in low or 'credential' in low or 'logon' in low:
            text += ' (check the WinRM username/password and that the account is in the JEA AllowedPrincipal)'
        elif 'refused' in low or 'timed out' in low or 'timeout' in low:
            text += ' (is the HTTPS listener on 5986 up and the firewall open from NetBox?)'
        elif 'configuration' in low or 'shell' in low:
            text += (f" (is the JEA endpoint '{self.configuration_name}' registered? "
                     'Run windows/Install-NetboxDhcpReader.ps1)')
        return text

    def close(self):
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                pool.close()
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug(f'WinRM: closing runspace pool on {self.server.hostname} failed: {exc}')
        if self._cert_tempfile is not None:
            self._cert_tempfile.close()
            self._cert_tempfile = None

    def __del__(self):  # pragma: no cover - interpreter shutdown ordering
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ invocation

    def _invoke(self, function: str, **params) -> List[str]:
        """Run one exported read function; returns its raw string output."""
        from pypsrp.powershell import PowerShell
        pool = self._open()
        ps = PowerShell(pool)
        ps.add_cmdlet(function)
        for key, value in params.items():
            if value is not None:
                ps.add_parameter(key, value)
        try:
            output = ps.invoke()
        except Exception as exc:
            self.close()
            raise PSUClientError(f'WinRM call {function} on {self.server.hostname} failed: {exc}') from exc
        if ps.had_errors:
            errors = '; '.join(str(e) for e in ps.streams.error) or 'unknown error'
            raise PSUClientError(f'{function} on {self.server.hostname} failed: {errors}')
        return [str(o) for o in output if o is not None]

    def _call(self, function: str, **params) -> Any:
        """Invoke and unwrap the module's {status, body, log} envelope (PSU-compatible semantics)."""
        raw = ''.join(self._invoke(function, **params)).strip()
        try:
            envelope = json.loads(raw)
            status = int(envelope['status'])
            body = envelope.get('body') or ''
        except (ValueError, KeyError, TypeError) as exc:
            raise PSUClientError(
                f'{function} on {self.server.hostname} returned an unexpected response '
                f'(is the NetboxDhcpReader module current?): {raw[:200]!r}'
            ) from exc
        for line in envelope.get('log') or []:
            logger.debug(f'[{self.server.hostname}] {line}')
        if status >= 400:
            try:
                message = json.loads(body).get('error', body)
            except (ValueError, AttributeError):
                message = body
            raise PSUClientError(f'{function} returned {status}: {message}', status_code=status)
        if not body:
            return None
        try:
            return json.loads(body)
        except ValueError as exc:
            raise PSUClientError(f'Invalid JSON from {function} on {self.server.hostname}: {exc}') from exc

    def _call_list(self, function: str, **params) -> List[Dict]:
        result = self._call(function, **params)
        if result is None:
            return []
        if isinstance(result, dict):
            return [result]
        return result

    def _refuse(self, op: str):
        raise PSUClientError(READ_ONLY_MESSAGE.format(op=op), status_code=403)

    # ------------------------------------------------------------------ scopes

    def list_scopes(self, active_only: bool = False, include_router: bool = True) -> List[Dict]:
        return self._call_list(
            'Get-NbDhcpScopes',
            active_only='true' if active_only else None,
            include_router=None if include_router else 'false',
        )

    def get_scope(self, scope_id: str) -> Dict:
        return self._call('Get-NbDhcpScope', scope_id=scope_id)

    def create_scope(self, payload: Dict) -> Dict:
        self._refuse('create scope')

    def update_scope(self, scope_id: str, payload: Dict) -> Dict:
        self._refuse('update scope')

    def delete_scope(self, scope_id: str) -> None:
        self._refuse('delete scope')

    # ------------------------------------------------------------------ leases

    def list_leases(self, scope_id: Optional[str] = None):
        if scope_id:
            return self._call_list('Get-NbDhcpLeases', scope_id=scope_id)
        result = self._call('Get-NbDhcpLeases', format='grouped')
        if isinstance(result, list) and result:
            raise PSUClientError('Bulk leases fetch returned a flat list; NetboxDhcpReader module is outdated')
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------ reservations

    def list_reservations(self, scope_id: Optional[str] = None):
        if scope_id:
            return self._call_list('Get-NbDhcpReservations', scope_id=scope_id)
        result = self._call('Get-NbDhcpReservations', format='grouped')
        if isinstance(result, list) and result:
            raise PSUClientError('Bulk reservations fetch returned a flat list; NetboxDhcpReader module is outdated')
        return result if isinstance(result, dict) else {}

    def create_reservation(self, payload: Dict) -> Dict:
        self._refuse('create reservation')

    def _refuse_batch(self, op: str, items: List[Dict]) -> List[Dict]:
        message = READ_ONLY_MESSAGE.format(op=op)
        return [_batch_error(item, message) for item in items]

    def create_reservations(self, items: List[Dict]) -> List[Dict]:
        return self._refuse_batch('create reservations', items)

    def update_reservations(self, items: List[Dict]) -> List[Dict]:
        return self._refuse_batch('update reservations', items)

    def delete_reservations(self, items: List[Dict]) -> List[Dict]:
        return self._refuse_batch('delete reservations', items)

    # ------------------------------------------------------------------ failover

    def list_failover(self) -> List[Dict]:
        return self._call_list('Get-NbDhcpFailover')

    def create_failover(self, payload: Dict) -> Dict:
        self._refuse('create failover')

    def replicate_failover(self, scope_ids: List[str]) -> Dict:
        self._refuse('replicate failover')

    # ------------------------------------------------------------------ options

    def list_server_options(self) -> List[Dict]:
        return self._call_list('Get-NbDhcpServerOptions')

    def list_scope_options(self, scope_id: str) -> List[Dict]:
        return self._call_list('Get-NbDhcpScopeOptions', scope_id=scope_id)

    def list_all_scope_options(self) -> Dict:
        result = self._call('Get-NbDhcpOptions')
        if not isinstance(result, dict):
            return {}
        scope_options = result.get('scope_options', {})
        return scope_options if isinstance(scope_options, dict) else {}

    # ------------------------------------------------------------------ exclusions

    def list_exclusions(self, scope_id: str) -> List[Dict]:
        return self._call_list('Get-NbDhcpExclusions', scope_id=scope_id)

    def list_all_exclusions(self) -> Dict:
        result = self._call('Get-NbDhcpExclusions')
        return result if isinstance(result, dict) else {}

    def create_exclusion(self, payload: Dict) -> Dict:
        self._refuse('create exclusion')

    def delete_exclusion(self, payload: Dict) -> None:
        self._refuse('delete exclusion')

    # ------------------------------------------------------------------ health

    def ping_read(self) -> Dict:
        return self._call('Get-NbDhcpHealth')

    def ping_write(self) -> bool:
        self._refuse('write access')

    # ------------------------------------------------------------------ PSU management (n/a)

    def _not_psu(self, *args, **kwargs):
        raise PSUClientError(
            'Not applicable with the WinRM transport: there are no PowerShell Universal endpoints. '
            'Update the NetboxDhcpReader module on the server with windows/Install-NetboxDhcpReader.ps1.',
            status_code=400,
        )

    get_dhcp_endpoints = _not_psu
    update_endpoint = _not_psu
    create_endpoint = _not_psu
    delete_endpoint = _not_psu
    restart_endpoints = _not_psu
