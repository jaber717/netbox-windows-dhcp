"""
Transport selection: how the plugin talks to the Windows DHCP servers.

    PLUGINS_CONFIG['netbox_windows_dhcp']['transport'] =
        'psu'    (default) PowerShell Universal REST endpoints, as upstream.
        'winrm'  WinRM / PowerShell remoting into a JEA endpoint running the plugin's own
                 PowerShell read functions (windows/NetboxDhcpReader). No PowerShell Universal.

The WinRM transport is READ-ONLY by design: the DHCP servers are the source of truth and
nothing is ever written to them. Read-only is enforced at four levels:
  1. push_reservations / push_scope_info are forced off in DHCPPluginSettings.load() and
     locked on the Settings page (effective_overrides below);
  2. every write method of WinRMClient refuses with a 403 PSUClientError;
  3. the JEA role on each server exposes only the Get-NbDhcp* read functions;
  4. the JEA virtual account is only in the local "DHCP Users" group (read-only in Windows).
"""
from django.conf import settings

PLUGIN = 'netbox_windows_dhcp'
TRANSPORT_PSU = 'psu'
TRANSPORT_WINRM = 'winrm'
TRANSPORTS = (TRANSPORT_PSU, TRANSPORT_WINRM)

# Settings that would write to a DHCP server; forced off with a read-only transport.
WRITE_SETTINGS = {
    'push_reservations': False,
    'push_scope_info': False,
}


def plugin_config():
    return getattr(settings, 'PLUGINS_CONFIG', {}).get(PLUGIN, {}) or {}


def transport_name():
    name = str(plugin_config().get('transport', TRANSPORT_PSU)).strip().lower()
    if name not in TRANSPORTS:
        raise ValueError(
            f"PLUGINS_CONFIG['{PLUGIN}']['transport'] must be one of {', '.join(TRANSPORTS)}, got {name!r}"
        )
    return name


def is_read_only_transport():
    try:
        return transport_name() == TRANSPORT_WINRM
    except ValueError:
        # A misconfigured transport must never unlock writes.
        return True


def effective_overrides(plugin_cfg=None):
    """
    The PLUGINS_CONFIG boolean overrides in force, keyed by PLUGINS_CONFIG key
    (sync_ips_from_dhcp, push_reservations, push_scope_info). A read-only transport adds
    push_* = False, which wins over anything configured.
    """
    if plugin_cfg is None:
        plugin_cfg = plugin_config()
    overrides = {
        key: bool(plugin_cfg[key])
        for key in ('sync_ips_from_dhcp', 'push_reservations', 'push_scope_info')
        if plugin_cfg.get(key) is not None
    }
    if is_read_only_transport():
        overrides.update(WRITE_SETTINGS)
    return overrides
