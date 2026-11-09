"""
WinRM / JEA read-only transport.

Most tests run the REAL generated module (windows/NetboxDhcpReader) in PowerShell 7 against a
fake DhcpServer module (tests/winrm_fake), with WinRMClient._invoke replaced by a local pwsh
call. That covers: plugin PowerShell bodies -> envelope -> WinRMClient -> plugin import/sync ->
NetBox objects. Only the WinRM wire (pypsrp) and the real DhcpServer cmdlets are not exercised.

The test copy of the module drops the [CimInstance] parameter type constraints, because CIM
objects cannot be created outside Windows; nothing else is changed.

Needs pwsh on PATH (or NETBOX_DHCP_TEST_PWSH); those tests are skipped otherwise.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock, skipUnless

from django.test import TestCase, override_settings
from ipam.models import IPAddress

from ..api_client import PSUClient, PSUClientError, get_client
from ..constants import PSU_SCRIPT_VERSION
from ..models import DHCPPluginSettings, DHCPScope, DHCPServer
from ..winrm_client import DEFAULT_PORT, WinRMClient
from .base import NULL_LOGGER, make_server, set_plugin_settings

REPO = Path(__file__).resolve().parents[2]
MODULE_DIR = REPO / 'windows' / 'NetboxDhcpReader'
FAKE_DIR = Path(__file__).resolve().parent / 'winrm_fake'
PWSH = os.environ.get('NETBOX_DHCP_TEST_PWSH') or shutil.which('pwsh')

WINRM_CONFIG = {
    'netbox_windows_dhcp': {
        'transport': 'winrm',
        'winrm': {'username': 'CORP\\svc-netbox-dhcp', 'password': 'not-a-real-password', 'auth': 'ntlm'},
    }
}

FAKE_DATA = {
    'scopes': [
        {'ScopeId': '10.0.1.0', 'Name': 'Building A', 'StartRange': '10.0.1.10', 'EndRange': '10.0.1.254',
         'SubnetMask': '255.255.255.0', 'Description': 'Users', 'State': 'Active', 'LeaseDurationSeconds': 691200},
        {'ScopeId': '10.0.2.0', 'Name': 'Building B', 'StartRange': '10.0.2.10', 'EndRange': '10.0.2.200',
         'SubnetMask': '255.255.255.0', 'Description': '', 'State': 'Inactive', 'LeaseDurationSeconds': 86400},
    ],
    'leases': [
        {'IPAddress': '10.0.1.50', 'ClientId': 'aa-bb-cc-dd-ee-01', 'HostName': 'pc-01.corp.example',
         'ScopeId': '10.0.1.0', 'LeaseExpiryTime': '2026-11-20T08:00:00Z', 'AddressState': 'Active'},
        {'IPAddress': '10.0.1.51', 'ClientId': 'aa-bb-cc-dd-ee-02', 'HostName': 'printer-02.corp.example',
         'ScopeId': '10.0.1.0', 'LeaseExpiryTime': '2026-11-20T09:30:00Z', 'AddressState': 'Active'},
        {'IPAddress': '10.0.1.52', 'ClientId': 'aa-bb-cc-dd-ee-03', 'HostName': 'old-laptop',
         'ScopeId': '10.0.1.0', 'LeaseExpiryTime': '2026-10-01T00:00:00Z', 'AddressState': 'Expired'},
        {'IPAddress': '10.0.2.20', 'ClientId': 'aa-bb-cc-dd-ee-04', 'HostName': 'cam-04',
         'ScopeId': '10.0.2.0', 'LeaseExpiryTime': '2026-11-20T10:00:00Z', 'AddressState': 'Active'},
    ],
    'reservations': [
        {'IPAddress': '10.0.1.100', 'ClientId': 'aa-bb-cc-dd-ee-99', 'Name': 'copier-1', 'Description': 'Floor 2',
         'Type': 'Both', 'ScopeId': '10.0.1.0'},
    ],
    'exclusions': [
        {'ScopeId': '10.0.1.0', 'StartRange': '10.0.1.240', 'EndRange': '10.0.1.250'},
    ],
    'options': {
        'server': [{'OptionId': 6, 'Name': 'DNS Servers', 'Value': ['10.0.0.53', '10.0.0.54'],
                    'Type': 'IPv4Address', 'VendorClass': ''}],
        'scope': {
            '10.0.1.0': [
                {'OptionId': 3, 'Name': 'Router', 'Value': ['10.0.1.1'], 'Type': 'IPv4Address', 'VendorClass': ''},
                {'OptionId': 15, 'Name': 'DNS Domain Name', 'Value': ['corp.example'], 'Type': 'String',
                 'VendorClass': ''},
            ],
            '10.0.2.0': [
                {'OptionId': 3, 'Name': 'Router', 'Value': ['10.0.2.1'], 'Type': 'IPv4Address', 'VendorClass': ''},
            ],
        },
    },
    'failover': [],
    'errors': {},
}


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


class PwshServer:
    """Runs the generated module in pwsh against the fake DhcpServer module."""

    def __init__(self, data):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        mod = root / 'NetboxDhcpReader'
        mod.mkdir()
        psm1 = (MODULE_DIR / 'NetboxDhcpReader.psm1').read_text()
        psm1 = psm1.replace('[Microsoft.Management.Infrastructure.CimInstance]', '')
        (mod / 'NetboxDhcpReader.psm1').write_text(psm1)
        shutil.copy(MODULE_DIR / 'NetboxDhcpReader.psd1', mod)
        shutil.copytree(FAKE_DIR / 'DhcpServer', root / 'DhcpServer')
        self.data_file = root / 'data.json'
        self.set_data(data)
        self.root = root
        self.calls = []

    def set_data(self, data):
        self.data_file.write_text(json.dumps(data))

    def close(self):
        self.tmp.cleanup()

    def invoke(self, client, function, **params):
        self.calls.append((function, {k: v for k, v in params.items() if v is not None}))
        args = ' '.join(f'-{k} {ps_quote(v)}' for k, v in params.items() if v is not None)
        script = (
            "$ErrorActionPreference = 'Stop'; "
            f"$env:PSModulePath = {ps_quote(str(self.root))} + [IO.Path]::PathSeparator + $env:PSModulePath; "
            "Import-Module NetboxDhcpReader -Force; "
            f"{function} {args}"
        )
        env = dict(os.environ, NBDHCP_FAKE_DATA=str(self.data_file))
        proc = subprocess.run(
            [PWSH, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script],
            capture_output=True, text=True, env=env, timeout=120,
        )
        if proc.returncode != 0:
            raise PSUClientError(f'pwsh failed: {proc.stderr.strip()[:500]}')
        return [line for line in proc.stdout.splitlines() if line.strip()]


def winrm_server(**kwargs):
    defaults = dict(name='DHCP01', hostname='dhcp01.corp.example', port=5986, use_https=True, verify_ssl=True)
    defaults.update(kwargs)
    return make_server(**defaults)


# ---------------------------------------------------------------------------
# Transport selection and read-only enforcement (no pwsh needed)
# ---------------------------------------------------------------------------

class TransportSelectionTests(TestCase):

    def test_default_transport_is_psu(self):
        server = winrm_server()
        self.assertIsInstance(get_client(server), PSUClient)

    @override_settings(PLUGINS_CONFIG=WINRM_CONFIG)
    def test_winrm_transport_selected(self):
        self.assertIsInstance(get_client(winrm_server()), WinRMClient)

    @override_settings(PLUGINS_CONFIG={'netbox_windows_dhcp': {'transport': 'smoke-signals'}})
    def test_unknown_transport_is_an_error_and_still_read_only(self):
        from ..transport import is_read_only_transport
        with self.assertRaises(ValueError):
            get_client(winrm_server())
        self.assertTrue(is_read_only_transport())
        self.assertFalse(DHCPPluginSettings.load().push_reservations)

    def test_client_parity_with_psu(self):
        """Every public PSUClient method exists on WinRMClient, so upstream additions are noticed."""
        public = {n for n in dir(PSUClient) if not n.startswith('_') and callable(getattr(PSUClient, n))}
        missing = sorted(n for n in public if not callable(getattr(WinRMClient, n, None)))
        self.assertEqual(missing, [])


@override_settings(PLUGINS_CONFIG=WINRM_CONFIG)
class ReadOnlyEnforcementTests(TestCase):

    def test_push_settings_forced_off_even_if_stored_on(self):
        DHCPPluginSettings.objects.update_or_create(pk=1, defaults={'push_reservations': True, 'push_scope_info': True})
        cfg = DHCPPluginSettings.load()
        self.assertFalse(cfg.push_reservations)
        self.assertFalse(cfg.push_scope_info)

    def test_push_settings_forced_off_even_if_config_says_on(self):
        conf = {'netbox_windows_dhcp': dict(
            WINRM_CONFIG['netbox_windows_dhcp'], push_reservations=True, push_scope_info=True)}
        with override_settings(PLUGINS_CONFIG=conf):
            cfg = DHCPPluginSettings.load()
        self.assertFalse(cfg.push_reservations)
        self.assertFalse(cfg.push_scope_info)

    def test_settings_form_locks_push_fields(self):
        from ..forms import PluginSettingsForm
        form = PluginSettingsForm(instance=DHCPPluginSettings.load())
        self.assertTrue(form.fields['push_reservations'].disabled)
        self.assertTrue(form.fields['push_scope_info'].disabled)
        self.assertFalse(form.fields['sync_ip_addresses'].disabled)

    def test_every_write_refused_with_403(self):
        client = WinRMClient(winrm_server())
        calls = [
            lambda: client.create_scope({}), lambda: client.update_scope('10.0.1.0', {}),
            lambda: client.delete_scope('10.0.1.0'), lambda: client.create_reservation({}),
            lambda: client.create_failover({}), lambda: client.replicate_failover(['10.0.1.0']),
            lambda: client.create_exclusion({}), lambda: client.delete_exclusion({}),
            client.ping_write,
        ]
        with mock.patch.object(WinRMClient, '_invoke', side_effect=AssertionError('no remote call expected')):
            for call in calls:
                with self.assertRaises(PSUClientError) as ctx:
                    call()
                self.assertEqual(ctx.exception.status_code, 403)
            item = {'scope_id': '10.0.1.0', 'ip_address': '10.0.1.9'}
            for batch in (client.create_reservations, client.update_reservations, client.delete_reservations):
                results = batch([item])
                self.assertEqual(results[0]['status'], 'error')
                self.assertIn('Read-only', results[0]['error'])

    def test_reserved_ip_save_does_not_queue_a_push(self):
        from ..signals import _reservation_push_enabled
        DHCPPluginSettings.objects.update_or_create(pk=1, defaults={'push_reservations': True})
        self.assertIsNone(_reservation_push_enabled())

    def test_missing_credentials_rejected(self):
        conf = {'netbox_windows_dhcp': {'transport': 'winrm'}}
        with override_settings(PLUGINS_CONFIG=conf):
            with self.assertRaisesRegex(PSUClientError, 'credentials missing'):
                WinRMClient(winrm_server())

    def test_per_server_credentials_override(self):
        conf = {'netbox_windows_dhcp': dict(WINRM_CONFIG['netbox_windows_dhcp'], server_overrides={
            'dhcp01.corp.example': {'winrm_username': 'CORP\\other', 'winrm_password': 'x'}})}
        with override_settings(PLUGINS_CONFIG=conf):
            client = WinRMClient(winrm_server())
        self.assertEqual(client.username, 'CORP\\other')

    def test_wsman_connection_parameters(self):
        server = winrm_server(port=443, ca_cert='-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n')
        client = WinRMClient(server)
        with mock.patch('pypsrp.wsman.WSMan') as wsman, mock.patch('pypsrp.powershell.RunspacePool') as pool:
            client._open()
        kwargs = wsman.call_args.kwargs
        self.assertEqual(wsman.call_args.args[0], 'dhcp01.corp.example')
        self.assertEqual(kwargs['port'], DEFAULT_PORT, '443 (the PSU default) is mapped to 5986')
        self.assertTrue(kwargs['ssl'])
        self.assertTrue(kwargs['no_proxy'], 'WinRM must not go through the https_proxy used for GitHub')
        self.assertEqual(kwargs['auth'], 'ntlm')
        self.assertTrue(str(kwargs['cert_validation']).endswith('.pem'))
        self.assertEqual(pool.call_args.kwargs['configuration_name'], 'NetboxDhcpReader')
        client.close()

    def test_connection_failure_is_a_client_error_with_hint(self):
        client = WinRMClient(winrm_server())
        with mock.patch('pypsrp.wsman.WSMan', side_effect=ConnectionError('Connection refused')):
            with self.assertRaisesRegex(PSUClientError, '5986 up and the firewall'):
                client.ping_read()


@skipUnless(MODULE_DIR.exists(), 'repository checkout needed (windows/ is not in the wheel)')
class GeneratedModuleTests(TestCase):

    def test_module_is_current(self):
        proc = subprocess.run([sys.executable, str(REPO / 'tools' / 'build_winrm_module.py'), '--check'],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_exported_functions_never_call_dhcp_write_cmdlets(self):
        text = (MODULE_DIR / 'NetboxDhcpReader.psm1').read_text()
        bodies = re.findall(r'^function (Get-NbDhcp\w+) \{(.*?)^\}', text, re.S | re.M)
        self.assertEqual(len(bodies), 10)
        for name, body in bodies:
            writes = re.findall(r'\b(?:Add|Set|Remove|Invoke|New|Rename)-DhcpServer\w*', body)
            self.assertEqual(writes, [], f'{name} calls write cmdlets: {writes}')
            write_helpers = (r'\b(Set-LeaseActiveReservation|Add-BatchReservationDetails'
                             r'|Write-BatchResults|Read-BatchBody)\b')
            helpers_used = set(re.findall(write_helpers, body))
            self.assertEqual(helpers_used, set(), f'{name} uses write helpers: {helpers_used}')

    def test_manifest_exports_only_read_functions(self):
        text = (MODULE_DIR / 'NetboxDhcpReader.psd1').read_text()
        exported = re.findall(r"'(\w+-\w+)'", text.split('FunctionsToExport', 1)[1].split(')', 1)[0])
        self.assertEqual(len(exported), 10)
        self.assertTrue(all(name.startswith('Get-NbDhcp') for name in exported), exported)


# ---------------------------------------------------------------------------
# Real module in pwsh
# ---------------------------------------------------------------------------

@skipUnless(PWSH and MODULE_DIR.exists(), 'pwsh and a repository checkout needed')
@override_settings(PLUGINS_CONFIG=WINRM_CONFIG)
class ModuleOutputTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = PwshServer(FAKE_DATA)

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def setUp(self):
        self.fake.set_data(FAKE_DATA)
        self.client = WinRMClient(winrm_server())
        patcher = mock.patch.object(WinRMClient, '_invoke', autospec=True, side_effect=self.fake.invoke)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_health_reports_plugin_script_version(self):
        self.assertEqual(self.client.ping_read(), {'status': 'ok', 'version': PSU_SCRIPT_VERSION})

    def test_scopes_shape(self):
        scopes = self.client.list_scopes()
        self.assertEqual([s['scope_id'] for s in scopes], ['10.0.1.0', '10.0.2.0'])
        a = scopes[0]
        self.assertEqual(a['name'], 'Building A')
        self.assertEqual((a['start_ip'], a['end_ip'], a['subnet_mask']), ('10.0.1.10', '10.0.1.254', '255.255.255.0'))
        self.assertEqual(a['lease_duration_seconds'], 691200)
        self.assertEqual(a['router'], '10.0.1.1')
        self.assertIsNone(a['failover_name'])
        self.assertEqual(a['state'], 'Active')

    def test_scopes_active_only_and_without_router(self):
        scopes = self.client.list_scopes(active_only=True, include_router=False)
        self.assertEqual([s['scope_id'] for s in scopes], ['10.0.1.0'])
        self.assertNotIn('router', scopes[0])

    def test_bulk_leases_grouped_active_only(self):
        leases = self.client.list_leases()
        self.assertEqual(sorted(leases), ['10.0.1.0', '10.0.2.0'])
        ips = [lease['ip_address'] for lease in leases['10.0.1.0']]
        self.assertEqual(ips, ['10.0.1.50', '10.0.1.51'], 'expired leases are not returned')
        first = leases['10.0.1.0'][0]
        self.assertEqual(first['client_id'], 'aa-bb-cc-dd-ee-01')
        self.assertEqual(first['hostname'], 'pc-01.corp.example')
        self.assertEqual(first['lease_expiry'], '2026-11-20T08:00:00Z')
        self.assertEqual(first['address_state'], 'Active')

    def test_single_scope_leases_flat(self):
        leases = self.client.list_leases(scope_id='10.0.2.0')
        self.assertEqual([lease['hostname'] for lease in leases], ['cam-04'])

    def test_reservations_grouped(self):
        res = self.client.list_reservations()
        self.assertEqual(res['10.0.1.0'][0]['name'], 'copier-1')
        self.assertEqual(res['10.0.2.0'], [])

    def test_exclusions_and_options(self):
        self.assertEqual(self.client.list_all_exclusions(),
                         {'10.0.1.0': [{'scope_id': '10.0.1.0', 'start_ip': '10.0.1.240', 'end_ip': '10.0.1.250'}]})
        self.assertEqual(self.client.list_exclusions('10.0.1.0')[0]['end_ip'], '10.0.1.250')
        opts = self.client.list_all_scope_options()
        self.assertEqual([o['code'] for o in opts['10.0.1.0']], [3, 15])
        self.assertEqual(self.client.list_server_options()[0]['value'], ['10.0.0.53', '10.0.0.54'])
        self.assertEqual(self.client.list_scope_options('10.0.2.0')[0]['value'], ['10.0.2.1'])
        self.assertEqual(self.client.list_failover(), [])

    def test_invalid_input_is_400(self):
        with self.assertRaises(PSUClientError) as ctx:
            self.client.get_scope("10.0.1.0'; Remove-Item C:\\ -Recurse; '")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_cmdlet_failure_is_500_with_message(self):
        self.fake.set_data(dict(FAKE_DATA, errors={'Get-DhcpServerv4Lease': 'RPC server unavailable'}))
        with self.assertRaises(PSUClientError) as ctx:
            self.client.list_leases()
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn('RPC server unavailable', str(ctx.exception))


# ---------------------------------------------------------------------------
# End to end: real module -> WinRMClient -> plugin import + sync -> NetBox
# ---------------------------------------------------------------------------

@skipUnless(PWSH and MODULE_DIR.exists(), 'pwsh and a repository checkout needed')
@override_settings(PLUGINS_CONFIG=WINRM_CONFIG)
class EndToEndReadOnlyTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = PwshServer(FAKE_DATA)

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def setUp(self):
        self.fake.set_data(FAKE_DATA)
        self.fake.calls.clear()
        patcher = mock.patch.object(WinRMClient, '_invoke', autospec=True, side_effect=self.fake.invoke)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Stored settings say "push"; the read-only transport must win.
        set_plugin_settings(sync_ip_addresses=True, push_reservations=True, push_scope_info=True,
                            create_missing_prefixes=True)
        self.server = winrm_server()

    def _sync(self):
        from ..background_tasks import _sync_server
        cfg = DHCPPluginSettings.load()
        return _sync_server(
            NULL_LOGGER, self.server,
            sync_ip_addresses=cfg.sync_ip_addresses,
            push_reservations=cfg.push_reservations,
            push_scope_info=cfg.push_scope_info,
        )

    def test_health_check_marks_server_read_only(self):
        from ..background_tasks import _check_server_health
        job = mock.Mock()
        _check_server_health(NULL_LOGGER, job, self.server)
        self.server.refresh_from_db()
        self.assertEqual(self.server.health_status, DHCPServer.HEALTH_HEALTHY)
        self.assertEqual(self.server.access_level, DHCPServer.ACCESS_RO)
        self.assertEqual(self.server.psu_script_version, PSU_SCRIPT_VERSION)

    def test_import_then_sync_shows_leases_macs_and_hostnames(self):
        from ..import_logic import run_import
        results = run_import(self.server)
        self.assertEqual(results['scopes']['errors'], [])
        self.assertEqual(sorted(DHCPScope.objects.values_list('name', flat=True)), ['Building A', 'Building B'])
        scope_a = DHCPScope.objects.get(name='Building A')
        self.assertEqual(str(scope_a.start_ip).split('/')[0], '10.0.1.10')

        self._sync()

        pc = IPAddress.objects.get(address__net_host='10.0.1.50')
        self.assertEqual(pc.status, 'dhcp')
        self.assertEqual(pc.dns_name, 'pc-01.corp.example')
        self.assertEqual(pc.custom_field_data.get('dhcp_client_id'), 'aa-bb-cc-dd-ee-01')
        self.assertTrue(IPAddress.objects.filter(address__net_host='10.0.1.51', status='dhcp').exists())
        self.assertFalse(IPAddress.objects.filter(address__net_host='10.0.1.52').exists(), 'expired lease not shown')
        copier = IPAddress.objects.get(address__net_host='10.0.1.100')
        self.assertEqual(copier.status, 'reserved')
        self.assertEqual(copier.custom_field_data.get('dhcp_client_id'), 'aa-bb-cc-dd-ee-99')

        called = {name for name, _ in self.fake.calls}
        self.assertTrue(called <= {'Get-NbDhcpHealth', 'Get-NbDhcpScopes', 'Get-NbDhcpScope', 'Get-NbDhcpLeases',
                                   'Get-NbDhcpReservations', 'Get-NbDhcpFailover', 'Get-NbDhcpOptions',
                                   'Get-NbDhcpServerOptions', 'Get-NbDhcpScopeOptions', 'Get-NbDhcpExclusions'},
                        called)

    def test_lease_that_disappears_is_removed_on_next_sync(self):
        from ..import_logic import run_import
        run_import(self.server)
        self._sync()
        self.assertTrue(IPAddress.objects.filter(address__net_host='10.0.1.51').exists())
        data = json.loads(json.dumps(FAKE_DATA))
        data['leases'] = [lease for lease in data['leases'] if lease['IPAddress'] != '10.0.1.51']
        self.fake.set_data(data)
        self._sync()
        self.assertFalse(IPAddress.objects.filter(address__net_host='10.0.1.51').exists())
        self.assertTrue(IPAddress.objects.filter(address__net_host='10.0.1.50').exists())

    def test_failed_read_deletes_nothing(self):
        from ..import_logic import run_import
        run_import(self.server)
        self._sync()
        before = set(IPAddress.objects.values_list('address', flat=True))
        self.fake.set_data(dict(FAKE_DATA, errors={'Get-DhcpServerv4Lease': 'RPC server unavailable'}))
        self._sync()
        self.assertEqual(set(IPAddress.objects.values_list('address', flat=True)), before)
