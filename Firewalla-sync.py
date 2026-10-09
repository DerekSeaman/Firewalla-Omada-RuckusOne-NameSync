#!/usr/bin/env python3
"""Firewalla MAC Sync

Syncs device names from the Firewalla cloud API to network management platforms.

Currently supported platforms
------------------------------
  omada   TP-Link Omada controller (via direct REST API)
  ruckus  Ruckus One cloud controller (via REST API)

Adding a new platform
---------------------
1.  Subclass ``Platform`` and set ``platform_name``.
2.  Implement ``set_device_name(mac, name) -> str``:
      ``mac``  is lowercase colon-separated.
      ``name`` is already sanitized by ``sanitize_name``.
      Return one of: ``'updated'``, ``'not_found'``, ``'failed'``.
3.  Override ``sanitize_name(name) -> str`` if the platform has naming
    restrictions. Return an empty string if the name cannot be made
    valid — the device will be skipped and counted as failed.
4.  Override ``fetch_known_macs() -> set | None`` to restrict syncing to
    MACs your platform already knows about. Return ``None`` to attempt
    sync for every device; return a set to restrict to those MACs only.
5.  Override ``fetch_existing_names() -> dict[str, str]`` to enable skip-if-unchanged.
    Return a dict of lowercase colon-separated MAC -> current sanitized name
    (``_normalize_mac`` converts other MAC formats).
6.  Add required ``secrets.conf`` keys to ``PLATFORM_REQUIRED_KEYS``.
7.  Register your class in ``PLATFORM_REGISTRY``.

See the ``Platform`` docstring for full details.
"""

from __future__ import annotations

import argparse
import configparser
import os
import re
import sys
import time
import traceback
import warnings
from abc import ABC, abstractmethod

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_FILE = 'secrets.conf'
MAC_PATTERN    = re.compile(r'^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$')
MSP_ID_PATTERN = re.compile(r'^[a-zA-Z0-9-]+$')
_MAC_SEPARATORS = re.compile(r'[-:.]')
_MAC_HEX        = re.compile(r'^[0-9a-fA-F]{12}$')

# URL template for the Firewalla MSP device-list endpoint.
# FIREWALLA_MSP_ID in secrets.conf is substituted at runtime.
FIREWALLA_DEVICES_URL = 'https://{msp_id}.firewalla.net/v2/devices'

# Config keys always required, regardless of which platforms are selected
BASE_REQUIRED_KEYS = ['FIREWALLA_API_TOKEN', 'FIREWALLA_MSP_ID']

# Config keys required per platform — only validated when that platform is active
PLATFORM_REQUIRED_KEYS: dict[str, list[str]] = {
    'omada': ['OMADA_URL', 'OMADA_USERNAME', 'OMADA_PASSWORD'],
    'ruckus': [
        'RUCKUS_CLIENT_ID',
        'RUCKUS_CLIENT_SECRET',
        'RUCKUS_TENANT_ID',
        'RUCKUS_REGION',
    ],
}

_FIREWALLA_REQUEST_TIMEOUT = 10   # seconds for all Firewalla HTTP requests
_OMADA_REQUEST_TIMEOUT     = 10   # seconds for all Omada HTTP requests
_OMADA_NOT_FOUND_CODE      = -41011  # Omada error code when the client MAC is unknown
_OMADA_PAGE_SIZE           = 100
_RUCKUS_REQUEST_TIMEOUT    = 10   # seconds for all Ruckus HTTP requests
_RUCKUS_PAGE_SIZE          = 100

# Upper bound on pages fetched from any paginated endpoint, so an API that
# ignores the page parameter can't loop forever.
_MAX_PAGES = 1000

# Allowlist for RUCKUS_REGION — any other value is rejected at startup.
_RUCKUS_VALID_REGIONS     = frozenset({'us', 'eu', 'asia'})
# RUCKUS_TENANT_ID must be exactly 32 hex characters (as shown in the portal URL).
_RUCKUS_TENANT_ID_PATTERN = re.compile(r'^[0-9a-fA-F]{32}$')

# Process exit codes — used by sys.exit() and SyncError.exit_code.
EXIT_CONFIG_ERROR  = 1   # missing or invalid configuration
EXIT_AUTH_ERROR    = 2   # authentication failure
EXIT_API_ERROR     = 3   # network or API error
EXIT_SYNC_FAILURES = 4   # sync ran, but one or more devices failed to update
EXIT_UNEXPECTED    = 5   # internal error (bug) — a traceback is printed

# ---------------------------------------------------------------------------
# ANSI colour codes
# Automatically disabled when stdout is not a TTY (e.g. piped output, cron)
# or when the NO_COLOR environment variable is set (https://no-color.org)
# ---------------------------------------------------------------------------

_USE_COLOUR = sys.stdout.isatty() and not os.environ.get('NO_COLOR')
GREEN  = '\033[1;92m' if _USE_COLOUR else ''
YELLOW = '\033[1;93m' if _USE_COLOUR else ''
RED    = '\033[1;91m' if _USE_COLOUR else ''
CYAN   = '\033[1;96m' if _USE_COLOUR else ''
BOLD   = '\033[1m'    if _USE_COLOUR else ''
RESET  = '\033[0m'    if _USE_COLOUR else ''

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SyncError(Exception):
    """Fatal error raised by platform code; caught in main() to exit cleanly.

    Prefer raising this over calling sys.exit() inside platform methods so
    that callers can catch, log, and choose the exit code.
    """

    def __init__(self, message: str, exit_code: int = EXIT_CONFIG_ERROR) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------


def _request_with_retry(request_fn):
    """Call request_fn(), retrying once after 2 s on transient connection errors.

    Only connection-level errors (ConnectionError, Timeout) trigger a retry.
    HTTP error responses (4xx, 5xx) are returned as-is for the caller to handle.
    """
    try:
        return request_fn()
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        time.sleep(2)
        return request_fn()


def _normalize_mac(mac: object) -> str:
    """Return ``mac`` as lowercase colon-separated, or ``''`` if it isn't a MAC.

    Accepts colon-, hyphen- or dot-separated forms and bare hex, so MACs from
    platform APIs compare equal to the colon-separated Firewalla format.
    """
    if not isinstance(mac, str):
        return ''
    digits = _MAC_SEPARATORS.sub('', mac)
    if not _MAC_HEX.match(digits):
        return ''
    digits = digits.lower()
    return ':'.join(digits[i:i + 2] for i in range(0, 12, 2))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config() -> configparser.SectionProxy:
    """Load and return configuration from secrets.conf.

    configparser's DEFAULT section is special: keys defined under [DEFAULT]
    are merged into every other section and accessible via config['DEFAULT'].
    We use it as a flat key-value store, which is the simplest pattern for
    a single-section config file like secrets.conf.

    Interpolation is disabled so secrets containing ``%`` are read literally.
    """
    config = configparser.ConfigParser(interpolation=None)
    try:
        if not config.read(CONFIG_FILE):
            print(f"Error: Config file '{CONFIG_FILE}' not found.", file=sys.stderr)
            print(f"       Copy secrets.conf.example to {CONFIG_FILE} "
                  f"and fill in your values.", file=sys.stderr)
            sys.exit(EXIT_CONFIG_ERROR)
    except configparser.MissingSectionHeaderError:
        print(f"Error: '{CONFIG_FILE}' is missing a [DEFAULT] section header.",
              file=sys.stderr)
        print("       The first line of the file must be: [DEFAULT]", file=sys.stderr)
        sys.exit(EXIT_CONFIG_ERROR)
    except configparser.Error as e:
        print(f"Error: Could not parse '{CONFIG_FILE}': {e}", file=sys.stderr)
        sys.exit(EXIT_CONFIG_ERROR)

    _warn_if_config_readable_by_others()
    return config['DEFAULT']


def _warn_if_config_readable_by_others() -> None:
    """Warn (but continue) when secrets.conf is accessible to group or others."""
    if os.name != 'posix':
        return
    try:
        mode = os.stat(CONFIG_FILE).st_mode
    except OSError:
        return
    if mode & 0o077:
        print(f"Warning: '{CONFIG_FILE}' is readable by other users. "
              f"Restrict it with: chmod 600 {CONFIG_FILE}", file=sys.stderr)


def validate_config(config: configparser.SectionProxy, platforms: list[str]) -> None:
    """Exit with a clear message if any required config keys are missing."""
    required = list(BASE_REQUIRED_KEYS)
    for platform in platforms:
        required.extend(PLATFORM_REQUIRED_KEYS.get(platform, []))

    missing = [key for key in required if not config.get(key)]
    if missing:
        print("Error: The following required keys are missing from secrets.conf:",
              file=sys.stderr)
        for key in missing:
            print(f"  - {key}", file=sys.stderr)
        print("\nSee secrets.conf.example for a complete reference.", file=sys.stderr)
        sys.exit(EXIT_CONFIG_ERROR)


# ---------------------------------------------------------------------------
# Firewalla
# ---------------------------------------------------------------------------


def fetch_firewalla_devices(api_token: str, msp_id: str) -> list[tuple[str, str]]:
    """Fetch all devices from the Firewalla cloud API.

    Returns a list of (name, mac) tuples, filtered to valid MAC addresses and
    non-blank string names (surrounding whitespace is trimmed).

    ``msp_id`` is the subdomain portion of your MSP URL, e.g. ``your-msp-id``
    from ``https://your-msp-id.firewalla.net``.  Set it as ``FIREWALLA_MSP_ID``
    in secrets.conf.
    """
    if not MSP_ID_PATTERN.match(msp_id):
        raise SyncError(
            "Error: FIREWALLA_MSP_ID contains invalid characters "
            "(only letters, numbers, and hyphens are allowed).",
            EXIT_CONFIG_ERROR,
        )
    url = FIREWALLA_DEVICES_URL.format(msp_id=msp_id)
    headers = {'Authorization': f'Token {api_token}'}
    try:
        response = _request_with_retry(
            lambda: requests.get(url, headers=headers, timeout=_FIREWALLA_REQUEST_TIMEOUT)
        )
        response.raise_for_status()
        data = response.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        raise SyncError(
            f"Error: Failed to fetch Firewalla devices: {e}",
            EXIT_API_ERROR,
        ) from e

    if not isinstance(data, list):
        raise SyncError(
            f"Error: Unexpected Firewalla API response — "
            f"expected a list, got {type(data).__name__}.",
            EXIT_API_ERROR,
        )

    result = []
    for device in data:
        try:
            mac  = device.get('mac', '')
            name = device.get('name', '')
        except AttributeError:
            # Skip entries that aren't dicts (e.g. upstream schema drift)
            continue
        if not isinstance(name, str) or not isinstance(mac, str):
            continue
        name = name.strip()
        if name and MAC_PATTERN.match(mac):
            result.append((name, mac))
    return result


def drop_duplicate_macs(devices: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep the first device for each MAC, warning when duplicates disagree on the name.

    The same MAC can appear more than once when an MSP account has several
    Firewalla boxes that have each seen the device. Pushing every copy would
    flip the platform's name back and forth on each run.
    """
    first_name: dict[str, str] = {}
    unique = []
    for name, mac in devices:
        key = mac.lower()
        if key in first_name:
            if first_name[key] != name:
                print(f"Warning: {mac} appears more than once in Firewalla "
                      f"('{first_name[key]}', '{name}'); using '{first_name[key]}'.",
                      file=sys.stderr)
            continue
        first_name[key] = name
        unique.append((name, mac))
    return unique


# ---------------------------------------------------------------------------
# Platform base class
# ---------------------------------------------------------------------------


class Platform(ABC):
    """Abstract base class for network management platform integrations.

    Subclass this to add support for a new platform. See the module
    docstring for the step-by-step guide.
    """

    # Set True in a subclass to print each successfully updated device
    # (suppressed by --quiet).
    list_updated = False

    def __init__(
        self,
        config: configparser.SectionProxy,
        dry_run: bool = False,
        quiet: bool = False,
        force_all: bool = False,
    ) -> None:
        self.config = config
        self.dry_run = dry_run
        self.quiet = quiet
        self.force_all = force_all

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Human-readable display name shown in output headers and summaries."""

    def sanitize_name(self, name: str) -> str:
        """Sanitize a raw Firewalla device name for this platform.

        Override if the platform imposes naming restrictions (e.g. no spaces).
        Return an empty string if the name cannot be made valid — the device
        will be counted as failed and skipped.
        """
        return name

    def fetch_known_macs(self) -> set | None:
        """Return the set of lowercase MACs this platform knows about.

        ``None``  →  no filtering; sync is attempted for every Firewalla device.
        ``set``   →  only devices whose MAC is in the set are synced; all others
                     are reported as NOT FOUND without calling set_device_name.
        """
        return None

    def fetch_existing_names(self) -> dict[str, str]:
        """Return a dict of lowercase MAC → current sanitized name.

        Used for skip-if-unchanged optimisation. Devices whose current name
        already matches the sanitized Firewalla name will not be pushed again.
        Return ``{}`` (the default) to always push every candidate device.
        """
        return {}

    @abstractmethod
    def set_device_name(self, mac: str, name: str) -> str:
        """Push a sanitized device name to the platform.

        Args:
            mac:  Lowercase colon-separated MAC address.
            name: Already-sanitized device name (output of ``sanitize_name``).

        Returns one of:
            ``'updated'``    — name pushed successfully.
            ``'not_found'``  — MAC is not known to this platform.
            ``'failed'``     — any other error.
        """

    # ------------------------------------------------------------------
    # Sync orchestration — not normally overridden
    # ------------------------------------------------------------------

    def sync(self, devices: list[tuple[str, str]]) -> int:
        """Sync device names to this platform and print a live summary.

        Returns the number of devices that failed to update.
        """
        total = len(devices)
        print(f"\n--- {self.platform_name} ---")

        known_macs     = self.fetch_known_macs()
        existing_names = self.fetch_existing_names()

        print(f"Syncing to {self.platform_name}...")
        updated = unchanged = not_found = failed = 0

        for name, mac in devices:
            mac_lower = mac.lower()

            # Skip if platform doesn't know this device
            if known_macs is not None and mac_lower not in known_macs:
                not_found += 1
                if not self.quiet:
                    print(f"  {YELLOW}[NOT FOUND]{RESET} {mac}  {name}")
                continue

            sanitized = self.sanitize_name(name)
            if not sanitized:
                failed += 1
                print(f"  {RED}[FAILED]   {RESET} {mac}  {name}"
                      f"  (name empty after sanitization)")
                continue

            # Skip if already up to date
            if existing_names.get(mac_lower) == sanitized:
                unchanged += 1
                continue

            if self.dry_run:
                suffix = f"  →  '{sanitized}'" if sanitized != name else ''
                print(f"  [DRY RUN]  {mac}  {name}{suffix}")
                updated += 1
                continue

            result = self.set_device_name(mac_lower, sanitized)
            if result == 'updated':
                updated += 1
                if self.list_updated and not self.quiet:
                    print(f"  {GREEN}[UPDATED]  {RESET} {mac}  {sanitized}")
            elif result == 'not_found':
                not_found += 1
                if not self.quiet:
                    print(f"  {YELLOW}[NOT FOUND]{RESET} {mac}  {name}")
            else:
                failed += 1
                print(f"  {RED}[FAILED]   {RESET} {mac}  {name}")

        self._print_summary(total, updated, unchanged, not_found, failed)
        return failed

    def _print_summary(
        self,
        total: int,
        updated: int,
        unchanged: int,
        not_found: int,
        failed: int,
    ) -> None:
        print(
            f"  {BOLD}Summary{RESET} — "
            f"{GREEN}Updated {updated}/{total}{RESET}  |  "
            f"{CYAN}Unchanged: {unchanged}{RESET}  |  "
            f"{YELLOW}Not found: {not_found}{RESET}  |  "
            f"{RED}Failed: {failed}{RESET}"
        )


# ---------------------------------------------------------------------------
# Omada platform
# ---------------------------------------------------------------------------


class OmadaPlatform(Platform):
    """TP-Link Omada SDN controller via direct REST API.

    Required secrets.conf keys
    --------------------------
    OMADA_URL         Full URL of the controller, e.g. https://192.168.1.1:8043
    OMADA_USERNAME    Admin username
    OMADA_PASSWORD    Admin password
    OMADA_SITE        Site name to sync (default: Default)
    OMADA_VERIFY_SSL  Set to 'false' to skip TLS certificate verification (default: true)

    Controller firmware 5.1+ is required.
    """

    def __init__(
        self,
        config: configparser.SectionProxy,
        dry_run: bool = False,
        quiet: bool = False,
        force_all: bool = False,
    ) -> None:
        super().__init__(config, dry_run, quiet, force_all)
        self._authenticated = False
        self._controller_id: str | None = None
        self._site_id: str | None = None

        url = config.get('OMADA_URL', '').rstrip('/')
        if not url.lower().startswith(('http://', 'https://')):
            url = 'https://' + url
        self._url = url

        self._username = config.get('OMADA_USERNAME', '')
        self._password = config.get('OMADA_PASSWORD', '')
        self._site_name = config.get('OMADA_SITE', 'Default')
        self._verify_ssl = (
            config.get('OMADA_VERIFY_SSL', 'true').lower().strip() != 'false'
        )

        # Persistent session for cookie-based auth and HTTP keep-alive
        self._session = requests.Session()

    @property
    def platform_name(self) -> str:
        return 'Omada'

    def sanitize_name(self, name: str) -> str:
        """Strip leading hyphens — the Omada API treats them as invalid."""
        return name.lstrip('-')

    # ------------------------------------------------------------------
    # Internal REST helpers
    # ------------------------------------------------------------------

    def _api_url(self, endpoint: str, site: bool = False) -> str:
        """Build an Omada controller API v2 URL."""
        base = f"{self._url}/{self._controller_id}/api/v2"
        if site:
            return f"{base}/sites/{self._site_id}/{endpoint}"
        return f"{base}/{endpoint}"

    def _request(self, method: str, url: str, **kwargs) -> dict:
        """Perform an Omada REST request and return the full parsed JSON body.

        Raises ``ValueError`` if the body is not a JSON object, so callers can
        rely on ``dict`` methods.

        When SSL verification is disabled, the InsecureRequestWarning is
        suppressed only for this call via a warnings context manager, avoiding
        the global side-effect of urllib3.disable_warnings().
        """
        kwargs['verify'] = self._verify_ssl
        kwargs.setdefault('timeout', _OMADA_REQUEST_TIMEOUT)
        if not self._verify_ssl:
            import urllib3  # noqa: PLC0415
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', urllib3.exceptions.InsecureRequestWarning)
                resp = _request_with_retry(
                    lambda: self._session.request(method, url, **kwargs)
                )
        else:
            resp = _request_with_retry(lambda: self._session.request(method, url, **kwargs))
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"expected a JSON object, got {type(data).__name__}")
        return data

    # ------------------------------------------------------------------
    # Authentication (cached after first call)
    # ------------------------------------------------------------------

    def _authenticate(self) -> None:
        """Log in to the Omada controller and resolve the target site ID.

        Three-step sequence:
          1. GET /api/info            — fetch controller ID (unauthenticated)
          2. POST /api/v2/login       — obtain CSRF token; session cookie set
          3. GET /api/v2/users/current — resolve site name → site ID
        """
        if self._authenticated:
            return

        print("  Authenticating...", end=' ', flush=True)

        # Step 1 — controller info
        try:
            data = self._request('get', f"{self._url}/api/info")
        except (requests.exceptions.RequestException, ValueError) as e:
            print("FAILED")
            raise SyncError(
                f"  Omada: could not reach controller at {self._url}: {e}",
                EXIT_AUTH_ERROR,
            ) from e
        try:
            self._controller_id = data['result']['omadacId']
        except (KeyError, TypeError):
            print("FAILED")
            raise SyncError(
                "  Omada: /api/info returned unexpected schema — "
                f"'result.omadacId' not found. Response: {str(data)[:200]}",
                EXIT_API_ERROR,
            )

        # Step 2 — login
        try:
            data = self._request(
                'post', self._api_url('login'),
                json={'username': self._username, 'password': self._password},
            )
        except (requests.exceptions.RequestException, ValueError) as e:
            print("FAILED")
            raise SyncError(f"  Omada login request failed: {e}", EXIT_AUTH_ERROR) from e

        code = data.get('errorCode', -1)
        if code != 0:
            print("FAILED")
            raise SyncError(
                f"  Omada login failed (code {code}): {data.get('msg', '')}",
                EXIT_AUTH_ERROR,
            )
        try:
            token = data['result']['token']
        except (KeyError, TypeError):
            print("FAILED")
            raise SyncError(
                "  Omada: login response missing token — unexpected API schema. "
                f"Response: {str(data)[:200]}",
                EXIT_API_ERROR,
            )
        self._session.headers.update({
            'Csrf-Token': token,
            'Omada-Request-Source': 'web-local',
            'Referer': self._url + '/',
            'Origin': self._url,
        })

        # Step 3 — resolve site name → site ID
        try:
            data = self._request('get', self._api_url('users/current'))
            result = data.get('result') or {}
            sites = (result.get('privilege') or {}).get('sites') or []
            sites = [s for s in sites if isinstance(s, dict)]
            self._site_id = next(
                (s.get('key') for s in sites if s.get('name') == self._site_name), None
            )
        except (requests.exceptions.RequestException, ValueError,
                AttributeError, TypeError) as e:
            print("FAILED")
            raise SyncError(f"  Omada site lookup failed: {e}", EXIT_AUTH_ERROR) from e

        if self._site_id is None:
            print("FAILED")
            available = ', '.join(str(s.get('name')) for s in sites)
            raise SyncError(
                f"  Omada site '{self._site_name}' not found. "
                f"Available sites: {available}",
                EXIT_CONFIG_ERROR,
            )

        self._authenticated = True
        print("OK")

    # ------------------------------------------------------------------
    # Platform hooks
    # ------------------------------------------------------------------

    def fetch_existing_names(self) -> dict[str, str]:
        """Fetch current client names for skip-if-unchanged detection.

        Authenticating here, before any device is processed, also means
        ``--dry-run`` verifies the controller URL, credentials and site name.

        If the client list can't be read, returns ``{}`` with a warning so
        every device is pushed (the pre-1.3.0 behaviour) instead of aborting.
        """
        self._authenticate()
        url = self._api_url('insight/clients', site=True)
        names: dict[str, str] = {}

        print("  Fetching existing client names...", end=' ', flush=True)
        try:
            for page in range(1, _MAX_PAGES + 1):
                data = self._request('get', url, params={
                    'currentPage': page, 'currentPageSize': _OMADA_PAGE_SIZE,
                })
                if data.get('errorCode', -1) != 0:
                    raise ValueError(f"error {data.get('errorCode')}: {data.get('msg', '')}")
                result = data.get('result') or {}
                rows = result.get('data') or []
                for client in rows:
                    if not isinstance(client, dict):
                        continue
                    mac  = _normalize_mac(client.get('mac'))
                    name = client.get('name')
                    if mac and isinstance(name, str):
                        names[mac] = name
                if not rows or page * _OMADA_PAGE_SIZE >= int(result.get('totalRows', 0)):
                    break
        except (requests.exceptions.RequestException, ValueError,
                AttributeError, TypeError) as e:
            print("skipped")
            print(f"  Warning: could not read Omada client names ({e}); "
                  f"pushing every device.", file=sys.stderr)
            return {}

        print(f"{len(names)} found.")
        return names

    def set_device_name(self, mac: str, name: str) -> str:
        """PATCH the client alias via the Omada REST API."""
        self._authenticate()
        mac_hyphen = mac.replace(':', '-')
        url = self._api_url(f"clients/{mac_hyphen}", site=True)
        try:
            data = self._request('patch', url, json={'name': name})
            code = data.get('errorCode', -1)
            if code == 0:
                return 'updated'
            if code == _OMADA_NOT_FOUND_CODE:
                return 'not_found'
            print(
                f"  Omada error {code} for {mac}: {data.get('msg', '')}",
                file=sys.stderr,
            )
            return 'failed'
        except (requests.exceptions.RequestException, ValueError) as e:
            print(f"  Warning: Omada request failed for {mac}: {e}", file=sys.stderr)
            return 'failed'


# ---------------------------------------------------------------------------
# Ruckus One platform
# ---------------------------------------------------------------------------

_RUCKUS_ALIAS_INVALID = re.compile(r'[^a-zA-Z0-9_-]')
_RUCKUS_MULTI_HYPHEN  = re.compile(r'-{2,}')


class RuckusPlatform(Platform):
    """Ruckus One cloud controller via REST API.

    Required secrets.conf keys
    --------------------------
    RUCKUS_CLIENT_ID      Application token client ID
    RUCKUS_CLIENT_SECRET  Application token client secret
    RUCKUS_TENANT_ID      32-character hex tenant ID (found in the portal URL)
    RUCKUS_REGION         Region code: us | eu | asia

    Create an Application Token in Ruckus One under:
    Administration → Account Management → Settings → Application Tokens
    """

    list_updated = True

    def __init__(
        self,
        config: configparser.SectionProxy,
        dry_run: bool = False,
        quiet: bool = False,
        force_all: bool = False,
    ) -> None:
        super().__init__(config, dry_run, quiet, force_all)
        self._authenticated = False
        self._reauthenticated = False

        region = config.get('RUCKUS_REGION', 'us').lower().strip()
        if region not in _RUCKUS_VALID_REGIONS:
            raise SyncError(
                f"Error: RUCKUS_REGION '{region}' is invalid. "
                f"Must be one of: {', '.join(sorted(_RUCKUS_VALID_REGIONS))}.",
                EXIT_CONFIG_ERROR,
            )

        tenant_id = config.get('RUCKUS_TENANT_ID', '')
        if not _RUCKUS_TENANT_ID_PATTERN.match(tenant_id):
            raise SyncError(
                "Error: RUCKUS_TENANT_ID must be a 32-character hexadecimal string "
                "(copy it from your Ruckus One portal URL).",
                EXIT_CONFIG_ERROR,
            )

        api_host  = 'api.ruckus.cloud'  if region == 'us' else f'api.{region}.ruckus.cloud'
        auth_host = 'ruckus.cloud'      if region == 'us' else f'{region}.ruckus.cloud'

        self._api_base = f'https://{api_host}'
        self._auth_url = f'https://{auth_host}/oauth2/token/{tenant_id}'

        # Persistent session for HTTP keep-alive across many sequential requests
        self._session = requests.Session()
        self._session.headers.update({'Accept': 'application/json'})

    @property
    def platform_name(self) -> str:
        return 'Ruckus One'

    # ------------------------------------------------------------------
    # Authentication (token is cached after first call)
    # ------------------------------------------------------------------

    def _authenticate(self, label: str = 'Authenticating...') -> None:
        """Obtain and cache a JWT access token via OAuth2 client credentials.

        The token is stored on ``self._session`` so all subsequent requests
        include it automatically.
        """
        if self._authenticated:
            return

        print(f"  {label}", end=' ', flush=True)
        # Don't send an old (possibly expired) bearer token to the token endpoint
        self._session.headers.pop('Authorization', None)
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json',
        }
        data = {
            'grant_type':    'client_credentials',
            'client_id':     self.config['RUCKUS_CLIENT_ID'],
            'client_secret': self.config['RUCKUS_CLIENT_SECRET'],
        }

        try:
            response = _request_with_retry(
                lambda: self._session.post(
                    self._auth_url, data=data, headers=headers,
                    timeout=_RUCKUS_REQUEST_TIMEOUT, allow_redirects=False,
                )
            )
        except (requests.exceptions.RequestException, ValueError) as e:
            print("FAILED")
            raise SyncError(f"  Authentication request failed: {e}", EXIT_AUTH_ERROR) from e

        if response.status_code in (301, 302, 303, 307, 308):
            print("FAILED")
            raise SyncError(
                "  Auth redirected — check client_id/client_secret.\n"
                "  Check: Administration → Account Management → "
                "Settings → Application Tokens",
                EXIT_AUTH_ERROR,
            )

        try:
            response.raise_for_status()
            token = response.json().get('access_token')
        except (requests.exceptions.HTTPError, ValueError, AttributeError) as e:
            print("FAILED")
            raise SyncError(f"  {e}", EXIT_AUTH_ERROR) from e

        if not token:
            print("FAILED")
            # Truncate to avoid leaking sensitive fields the server may return
            body_preview = response.text.strip()[:200]
            raise SyncError(
                f"  No access_token in response: {body_preview}",
                EXIT_AUTH_ERROR,
            )

        print("OK")
        self._session.headers.update({'Authorization': f'Bearer {token}'})
        self._authenticated = True

    def _send(self, method: str, url: str, **kwargs) -> requests.Response:
        """Send an authenticated API request and return the response.

        If the token expires mid-run (HTTP 401), re-authenticates and repeats
        the request — once per run, so a 401 caused by missing permissions
        doesn't trigger a token request for every device.
        """
        self._authenticate()
        kwargs.setdefault('timeout', _RUCKUS_REQUEST_TIMEOUT)
        response = _request_with_retry(lambda: self._session.request(method, url, **kwargs))
        if response.status_code == 401 and not self._reauthenticated:
            self._reauthenticated = True
            self._authenticated = False
            self._authenticate('Token rejected — re-authenticating...')
            response = _request_with_retry(lambda: self._session.request(method, url, **kwargs))
        return response

    # ------------------------------------------------------------------
    # Name sanitization
    # ------------------------------------------------------------------

    def sanitize_name(self, name: str) -> str:
        """Sanitize a name to meet Ruckus One alias requirements.

        Rules: must start with a letter or number; may only contain letters,
        numbers, hyphens, and underscores; 1–255 characters.
        Spaces become hyphens; other invalid characters are stripped;
        consecutive hyphens are collapsed.

        Returns an empty string if the result would be empty (e.g. the name
        consisted entirely of invalid characters). The caller will skip the
        device and count it as failed.
        """
        alias = name.replace(' ', '-')
        alias = _RUCKUS_ALIAS_INVALID.sub('', alias)
        alias = _RUCKUS_MULTI_HYPHEN.sub('-', alias)
        # Strip again after truncating so the alias can't end in '-' or '_'
        return alias.strip('-_')[:255].rstrip('-_')

    # ------------------------------------------------------------------
    # Platform hooks
    # ------------------------------------------------------------------

    def fetch_known_macs(self) -> set | None:
        """Return MAC addresses of devices that have connected to a Ruckus AP.

        Returns ``None`` when ``--force-all`` is set, which causes the sync
        engine to attempt every Firewalla device regardless of whether Ruckus
        has seen it before.
        """
        if self.force_all:
            print("  Force-all mode — skipping WiFi client filter, syncing all Firewalla devices.")
            return None

        self._authenticate()
        url = f'{self._api_base}/clients'
        macs: set[str] = set()

        print("  Fetching WiFi clients...", end=' ', flush=True)
        for page in range(_MAX_PAGES):
            try:
                response = self._send(
                    'get', url, params={'page': page, 'size': _RUCKUS_PAGE_SIZE},
                )
                response.raise_for_status()
                data = response.json()
            except (requests.exceptions.RequestException, ValueError) as e:
                print("FAILED")
                raise SyncError(f"  {e}", EXIT_API_ERROR) from e

            if not isinstance(data, list):
                print("FAILED")
                raise SyncError(
                    f"  /clients returned {type(data).__name__} instead of a list.",
                    EXIT_API_ERROR,
                )

            known_before = len(macs)
            for client in data:
                if isinstance(client, dict):
                    mac = _normalize_mac(client.get('mac'))
                    if mac:
                        macs.add(mac)

            # Stop on an empty page, or one that adds nothing new (the API
            # ignoring `page`). A short page is not the end: the API may cap
            # `size` below what was requested.
            if not data or len(macs) == known_before:
                break
        else:
            print(f"\n  Warning: stopped after {_MAX_PAGES} pages of WiFi clients.",
                  file=sys.stderr)

        print(f"{len(macs)} found.")
        return macs

    def fetch_existing_names(self) -> dict[str, str]:
        """Fetch all current client aliases for skip-if-unchanged detection."""
        self._authenticate()
        url = f'{self._api_base}/clients/aliases/query'
        aliases: dict[str, str] = {}

        print("  Fetching existing aliases...", end=' ', flush=True)
        for page in range(_MAX_PAGES):
            try:
                response = self._send(
                    'post', url, json={},
                    headers={'Content-Type': 'application/json'},
                    params={'page': page, 'size': _RUCKUS_PAGE_SIZE},
                )
                response.raise_for_status()
                data = response.json()
            except (requests.exceptions.RequestException, ValueError) as e:
                print("FAILED")
                raise SyncError(f"  {e}", EXIT_API_ERROR) from e

            if not isinstance(data, dict):
                print("FAILED")
                raise SyncError(
                    f"  /clients/aliases/query returned "
                    f"{type(data).__name__} instead of a dict.",
                    EXIT_API_ERROR,
                )

            for client in data.get('content') or []:
                if not isinstance(client, dict):
                    continue
                mac   = _normalize_mac(client.get('macAddress'))
                alias = client.get('alias')
                if mac:
                    aliases[mac] = alias if isinstance(alias, str) else ''

            if data.get('last', True):
                break
        else:
            print(f"\n  Warning: stopped after {_MAX_PAGES} pages of aliases.",
                  file=sys.stderr)

        print(f"{len(aliases)} found.")
        return aliases

    def set_device_name(self, mac: str, name: str) -> str:
        """Set a client alias in Ruckus One. ``name`` is already sanitized."""
        url     = f'{self._api_base}/clients/aliases/{mac}'
        # deviceType must be 'WIFI' — Ruckus One aliases are WiFi-only;
        # wired clients are not manageable via this endpoint.
        payload = {'alias': name, 'macAddress': mac, 'deviceType': 'WIFI'}

        try:
            response = self._send(
                'put', url, json=payload,
                headers={'Content-Type': 'application/json'},
            )
            # The API normally answers 202 Accepted; treat any 2xx as success
            if 200 <= response.status_code < 300:
                return 'updated'
            if response.status_code == 404:
                return 'not_found'
            print(f"  Warning: HTTP {response.status_code} for {mac}: "
                  f"{response.text[:200]}", file=sys.stderr)
            return 'failed'
        except requests.exceptions.RequestException as e:
            print(f"  Warning: request failed for {mac}: {e}", file=sys.stderr)
            return 'failed'


# ---------------------------------------------------------------------------
# Platform registry
# ---------------------------------------------------------------------------


# To register a new platform, add an entry here.
PLATFORM_REGISTRY: dict[str, type[Platform]] = {
    'omada':  OmadaPlatform,
    'ruckus': RuckusPlatform,
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    available = list(PLATFORM_REGISTRY.keys())
    parser = argparse.ArgumentParser(
        description='Sync Firewalla device names to network management platforms.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
platforms:
  omada    TP-Link Omada  (requires OMADA_* credentials in secrets.conf)
  ruckus   Ruckus One     (requires RUCKUS_* credentials in secrets.conf)

examples:
  python Firewalla-sync.py
  python Firewalla-sync.py --platform ruckus
  python Firewalla-sync.py --dry-run
  python Firewalla-sync.py --platform omada ruckus --dry-run
  python Firewalla-sync.py --quiet
  python Firewalla-sync.py --platform ruckus --force-all
  python Firewalla-sync.py --platform ruckus --force-all --dry-run

exit codes:
  0  success
  1  configuration error (missing or invalid config)
  2  authentication failure
  3  network or API error
  4  sync ran, but one or more devices failed to update
  5  unexpected internal error (a traceback is printed)
""",
    )
    parser.add_argument(
        '--platform',
        nargs='+',
        choices=available,
        default=available,
        metavar='PLATFORM',
        help=f"One or more platforms to sync (default: all). "
             f"Choices: {', '.join(available)}.",
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Preview what would change without pushing anything.',
    )
    parser.add_argument(
        '--quiet',
        action='store_true',
        help='Suppress per-device NOT FOUND and UPDATED output; show only '
             'summaries and failures.',
    )
    parser.add_argument(
        '--force-all',
        action='store_true',
        help=(
            'Ruckus only: push all Firewalla devices to Ruckus One, '
            'even if their MAC has never connected to a Ruckus AP. '
            'By default only known WiFi clients are synced.'
        ),
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_platform(name: str, config: configparser.SectionProxy,
                 args: argparse.Namespace, devices: list[tuple[str, str]]) -> int:
    """Build and sync one platform; return its exit code (0 on full success).

    Errors are reported here rather than raised, so one platform failing
    doesn't stop the others from syncing.
    """
    try:
        platform = PLATFORM_REGISTRY[name](config, args.dry_run, args.quiet, args.force_all)
        failed = platform.sync(devices)
    except SyncError as e:
        print(str(e), file=sys.stderr)
        return e.exit_code
    except Exception:
        print(f"Error: unexpected failure while syncing '{name}':", file=sys.stderr)
        traceback.print_exc()
        return EXIT_UNEXPECTED
    return EXIT_SYNC_FAILURES if failed else 0


def main() -> None:
    # Keep stdout and stderr lines in order when both go to one log (cron's 2>&1)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(line_buffering=True)
    args = parse_args()
    platforms = list(dict.fromkeys(args.platform))  # drop repeats, keep order
    config = load_config()
    validate_config(config, platforms)

    print("Fetching Firewalla devices...", end=' ', flush=True)
    try:
        devices = fetch_firewalla_devices(
            config['FIREWALLA_API_TOKEN'],
            config['FIREWALLA_MSP_ID'],
        )
    except SyncError as e:
        print("FAILED")
        print(str(e), file=sys.stderr)
        sys.exit(e.exit_code)
    except Exception:
        print("FAILED")
        traceback.print_exc()
        sys.exit(EXIT_UNEXPECTED)
    print(f"{len(devices)} found.")
    devices = drop_duplicate_macs(devices)

    if args.dry_run:
        print(f"{YELLOW}Dry-run mode — no changes will be made.{RESET}")
    if args.force_all and 'ruckus' not in platforms:
        print(
            f"{YELLOW}Warning: --force-all has no effect without --platform ruckus.{RESET}"
        )
    elif args.force_all:
        print(f"{YELLOW}Force-all mode — pushing all Firewalla devices to Ruckus One.{RESET}")

    # A platform-level error (config, auth, API, bug) outranks per-device
    # failures in the exit code; otherwise the first error wins.
    exit_code = 0
    for name in platforms:
        code = run_platform(name, config, args, devices)
        if code and exit_code in (0, EXIT_SYNC_FAILURES):
            exit_code = code

    if exit_code:
        print(f"\nDone with errors (exit code {exit_code}).")
        sys.exit(exit_code)
    print("\nDone.")


if __name__ == '__main__':
    main()
