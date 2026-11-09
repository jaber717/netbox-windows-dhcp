# WinRM transport (read-only, no PowerShell Universal)

This fork adds a second way to reach the Windows DHCP servers: **WinRM (PowerShell remoting)
into a JEA endpoint**, built into Windows, with no PowerShell Universal and no license.

It is **read-only**. NetBox imports scopes, options, exclusions and failover, and shows leases
and reservations as IP Addresses (MAC in *DHCP Client ID*, hostname in *DNS name*). Nothing is
ever written to a DHCP server.

## How read-only is enforced

| Layer | What stops a write |
| --- | --- |
| Plugin settings | With `transport: winrm`, *Push Reservations* and *Push Scope Info* are forced off in memory and locked on the Settings page, whatever is stored or configured. |
| NetBox client | Every write method of `WinRMClient` refuses with HTTP-like 403; the health check shows the server as **Read-only**. |
| JEA role | The endpoint exposes only the 10 `Get-NbDhcp*` functions. No other command can be run (`NoLanguage`). |
| Windows | The JEA session runs as a temporary virtual account that is only in the local **DHCP Users** group (read-only DHCP access), not Administrators. |

The `Get-NbDhcp*` functions are the plugin's own PowerShell Universal GET endpoints, copied
verbatim by `tools/build_winrm_module.py`, so the data is exactly what the PSU transport returns.

## 1. Windows DHCP server (each server)

Prerequisites: Windows Server 2016+ with the DHCP Server role, member server (not a domain
controller), and an AD account for NetBox, for example `CORP\svc-netbox-dhcp` (a normal user,
no admin rights). A group such as `CORP\NetBox-DHCP-Readers` works too.

Copy the repository's `windows` folder to the server, then in an **elevated Windows PowerShell 5.1**:

```powershell
cd C:\Temp\netbox-windows-dhcp\windows
Set-ExecutionPolicy -Scope Process Bypass
.\Install-NetboxDhcpReader.ps1 -AllowedPrincipal 'CORP\svc-netbox-dhcp' `
    -NetBoxAddress 10.10.20.15 -CreateSelfSignedCertificate
```

- `-NetBoxAddress`: NetBox's IP; the firewall rule for TCP 5986 allows only it.
- Certificate: `-CertificateThumbprint <thumbprint>` for an AD CS certificate (preferred), or
  `-CreateSelfSignedCertificate`, which also writes `C:\ProgramData\NetboxDhcpReader\winrm-<host>.cer`
  to paste into NetBox.
- `-SkipListener` if WinRM HTTPS is already managed by GPO.
- Every session is transcribed to `C:\ProgramData\NetboxDhcpReader\Transcripts` (audit trail).
- Remove everything: `.\Install-NetboxDhcpReader.ps1 -Uninstall`.

The script ends with a self-test through the endpoint (it works when your admin account is also
in the allowed principal; otherwise test from NetBox).

## 2. NetBox

Install the plugin from GitHub (`pypsrp` is installed with it):

```bash
export https_proxy=http://PROXY:PORT
sudo mkdir -p /opt/netbox-plugins
sudo -E git clone -b winrm-readonly https://github.com/jaber717/netbox-windows-dhcp.git /opt/netbox-plugins/netbox-windows-dhcp
sudo -E /opt/netbox/venv/bin/pip install /opt/netbox-plugins/netbox-windows-dhcp
```

Then configure NetBox with one command (no Python editing):

```bash
cd /opt/netbox-plugins/netbox-windows-dhcp
sudo bash contrib/apply-config.sh /opt/netbox            # asks for the account and password
sudo /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py migrate
sudo /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py collectstatic --no-input
sudo systemctl restart netbox netbox-rq
```

`apply-config.sh`:

- stores the password in `/etc/netbox/dhcp-winrm.env` (root only, base64 so any character
  works) and adds systemd drop-ins so `netbox` and `netbox-rq` load it; the password is never
  written to `configuration.py`;
- appends a marked block to `configuration.py` (plugin enabled, `transport: winrm`, account,
  auth); nothing else in the file is touched, so other plugins' settings stay as they are;
- backs everything up, runs `manage.py check` and restores everything if anything fails;
- can be re-run, e.g. after a password change (then restart the services).

Options: `--username 'CORP\svc-netbox-dhcp'`, `--auth ntlm|negotiate|kerberos`.

Test a server from the command line before adding it in the UI (read-only):

```bash
sudo bash contrib/test-winrm.sh /opt/netbox dhcp01.corp.example /path/to/winrm-DHCP01.cer
```

It prints health, scopes with lease counts, failover relationships, and confirms writes are refused.

<details><summary>Manual configuration instead of the script</summary>

```python
import os

PLUGINS = [..., 'netbox_windows_dhcp']
PLUGINS_CONFIG = {
    'netbox_windows_dhcp': {
        'transport': 'winrm',
        'winrm': {
            'username': 'CORP\\svc-netbox-dhcp',
            'password': os.environ.get('NETBOX_DHCP_WINRM_PASSWORD', ''),
            'auth': 'ntlm',            # or 'negotiate' / 'kerberos' (Kerberos needs krb5 + gssapi)
        },
        # optional, per DHCP server hostname:
        # 'server_overrides': {'dhcp02.corp.example': {'winrm_username': '...', 'winrm_password': '...'}},
    },
}
```

</details>

WinRM traffic never goes through `https_proxy`/`HTTP_PROXY` (DHCP servers are internal).
Set `'use_proxy': True` under `winrm` only if you really route WinRM through a proxy.

## 3. Add the server in NetBox

*Plugins → DHCP Servers → Add*: **Hostname** = the server's FQDN (must match the certificate),
**Port** = 5986 (443 is treated as 5986), **Use HTTPS** on, **Verify SSL** on, **CA certificate** =
your AD CS root, or the `.cer` file from step 1. Leave **App Token** empty.

Run the health check: the server should show **Healthy** and **Read-only**.

### Failover pairs

Install the JEA endpoint on **both** servers and add **both** in NetBox before importing.
The failover is matched by hostname: the primary reports its FQDN, the partner is reported
exactly as written in Windows (`Get-DhcpServerv4Failover | Select Name, PartnerServer`). Use that
exact string as the partner's Hostname in NetBox, and make sure the partner's certificate covers it.
A newly imported failover starts in maintenance mode and its scopes are skipped: check its Default
Scope VRF, take it out of maintenance, then run *Import from Server* again.

## 4. Safe first import

The plugin treats the DHCP server as the source of truth and also **changes NetBox**:

- *Sync IP Addresses* (off by default): inside each scope's start-end range, NetBox keeps only the
  IPs the server has a lease or reservation for; **other IPs in that range are deleted**,
  including hand-made ones, unless protected.
- *Create Missing Prefixes* (on by default): a scope without a matching NetBox prefix gets one,
  in the server's *Default Scope VRF*.

Recommended order:

1. Settings: leave **Sync IP Addresses off**. Set the server's **Default Scope VRF** if your
   DHCP prefixes live in a VRF.
2. Set a **Sync-Protected Tag** and put it on any prefix or IP you do not want the sync to touch.
3. *Import from Server*. Check the created scopes and prefixes.
4. Turn **Sync IP Addresses on**. Leases appear as IPs with status *DHCP*; reservations as *Reserved*.

A failed read from a server never deletes anything (upstream behaviour, covered by tests here too).

## Updating

After merging upstream changes: `python3 tools/build_winrm_module.py`, run the tests, and
re-run `Install-NetboxDhcpReader.ps1` on each server (it updates in place).
`python3 tools/build_winrm_module.py --check` fails if the module is stale; a test does the same.

## Tests and what they do not cover

`netbox_windows_dhcp/tests/test_winrm.py` runs the generated module in PowerShell 7 against a
fake `DhcpServer` module and feeds the result through `WinRMClient` into the plugin's real import
and sync code. Not covered here: the WinRM wire protocol, JEA itself and the real DhcpServer
cmdlets (they only exist on Windows). Validate on one DHCP server first.

Known limits: domain controllers (no virtual accounts / local groups) are not supported by the
installer; Kerberos needs extra packages on RHEL; whether *DHCP Users* can read failover state
should be confirmed on your servers (the health check and import will show an error if not).
