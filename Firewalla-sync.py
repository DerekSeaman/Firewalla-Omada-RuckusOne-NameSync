#!/usr/bin/env python3
"""Firewalla MAC Sync

Syncs device names from the Firewalla cloud API to network management platforms.

Currently supported platforms
------------------------------
  omada   TP-Link Omada controller (via tplink-omada-api CLI)
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
    Return a dict of lowercase MAC -> current sanitized name.
6.  Add required ``secrets.conf`` keys to ``PLATFORM_REQUIRED_KEYS``.
7.  Register your class in ``build_platforms()``.

See the ``Platform`` docstring for full details.
"""

from __future__ import annotations

import argparse
import configparser
import re
import subprocess
import sys
import time
from abc import ABC, abstractmethod

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_FILE = 'secrets.conf'
MAC_PATTERN    = re.compile(r'^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$')
MSP_ID_PATTERN = re.compile(r'^[a-zA-Z0-9-]+$')

# URL template for the Firewalla MSP device-list endpoint.
# FIREWALLA_MSP_ID in secrets.conf is substituted at runtime.
FIREWALLA_DEVICES_URL = 'https://{msp_id}.firewalla.net/v2/devices'

# Config keys always required, regardless of which platforms are selected
BASE_REQUIRED_KEYS = ['FIREWALLA_API_TOKEN', 'FIREWALLA_MSP_ID']

# Config keys required per platform — only validated when that platform is active
PLATFORM_REQUIRED_KEYS: dict[str, list[str]] = {
    # Omada uses the tplink-omada-api CLI, which manages its own connection config
    'omada': [],
    'ruckus': [
        'RUCKUS_CLIENT_ID',
        'RUCKUS_CLIENT_SECRET',
        'RUCKUS_TENANT_ID',
        'RUCKUS_REGION',
    ],
}

_OMADA_CLI_TIMEOUT      = 30   # seconds before a hung omada subprocess is killed
_RUCKUS_REQUEST_TIMEOUT = 10   # seconds for all Ruckus HTTP requests

# Allowlist for RUCKUS_REGION — any other value is rejected at startup.
_RUCKUS_VALID_REGIONS     = frozenset({'us', 'eu', 'asia'})
# RUCKUS_TENANT_ID must be exactly 32 hex characters (as shown in the portal URL).
_RUCKUS_TENANT_ID_PATTERN = re.compile(r'^[0-9a-fA-F]{32}$')

# Process exit codes — used by sys.exit() and SyncError.exit_code.
EXIT_OK           = 0
EXIT_CONFIG_ERROR = 1   # missing or invalid configuration
EXIT_AUTH_ERROR   = 2   # authentication failure
EXIT_API_ERROR    = 3   # network or API error

# ---------------------------------------------------------------------------
# ANSI colour codes
# Automatically disabled when stdout is not a TTY (e.g. piped output, cron)
# ---------------------------------------------------------------------------

_USE_COLOUR = sys.stdout.isatty()
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


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config() -> configparser.SectionProxy:
    """Load and return configuration from secrets.conf.

    configparser's DEFAULT section is special: keys defined under [DEFAULT]
    are merged into every other section and accessible via config['DEFAULT'].
    We use it as a flat key-value store, which is the simplest pattern for
    a single-section config file like secrets.conf.
    """
    config = configparser.ConfigParser()
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
    return config['DEFAULT']


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

    Returns a list of (name, mac) tuples, filtered to valid MAC addresses only.

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
            lambda: requests.get(url, headers=headers, timeout=10)
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
        if name and isinstance(mac, str) and MAC_PATTERN.match(mac):
            result.append((name, mac))
    return result


# ---------------------------------------------------------------------------
# Platform base class
# ---------------------------------------------------------------------------


class Platform(ABC):
    """Abstract base class for network management platform integrations.

    Subclass this to add support for a new platform. See the module
    docstring for the step-by-step guide.
    """

    def __init__(
        self,
        config: configparser.SectionProxy,
        dry_run: bool = False,
        quiet: bool = False,
    ) -> None:
        self.config = config
        self.dry_run = dry_run
        self.quiet = quiet

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

    def sync(self, devices: list[tuple[str, str]]) -> None:
        """Sync device names to this platform and print a live summary."""
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
            elif result == 'not_found':
                not_found += 1
                if not self.quiet:
                    print(f"  {YELLOW}[NOT FOUND]{RESET} {mac}  {name}")
            else:
                failed += 1
                print(f"  {RED}[FAILED]   {RESET} {mac}  {name}")

        self._print_summary(total, updated, unchanged, not_found, failed)

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
    """TP-Link Omada controller via the tplink-omada-api CLI.

    Requirements
    ------------
    - Install the CLI: https://github.com/MarkGodwin/tplink-omada-api
    - The ``omada`` command must be on your PATH and pre-configured with
      your controller address and credentials.

    Note: The Omada CLI does not provide a command to read current client
    names, so unchanged detection is not supported. Every run pushes all
    device names to the controller.
    """

    @property
    def platform_name(self) -> str:
        return 'Omada'

    def sanitize_name(self, name: str) -> str:
        """Strip leading hyphens to prevent the Omada CLI from interpreting
        a device name (e.g. ``-help``) as a command-line flag.
        """
        return name.lstrip('-')

    def set_device_name(self, mac: str, name: str) -> str:
        mac_hyphen = mac.replace(':', '-')
        try:
            result = subprocess.run(
                ['omada', 'set-client-name', '--', mac_hyphen, name],
                capture_output=True,
                text=True,
                timeout=_OMADA_CLI_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return 'failed'
        except FileNotFoundError:
            raise SyncError(
                "Error: 'omada' CLI not found. Is it installed and on your PATH?",
                EXIT_CONFIG_ERROR,
            )

        if result.returncode == 0:
            return 'updated'
        if '-41011' in result.stderr:
            return 'not_found'
        if result.stderr.strip():
            print(f"  Omada error: {result.stderr.strip()}", file=sys.stderr)
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

    def __init__(
        self,
        config: configparser.SectionProxy,
        dry_run: bool = False,
        quiet: bool = False,
    ) -> None:
        super().__init__(config, dry_run, quiet)
        self._authenticated = False

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

    def _authenticate(self) -> None:
        """Obtain and cache a JWT access token via OAuth2 client credentials.

        The token is stored on ``self._session`` so all subsequent requests
        include it automatically.
        """
        if self._authenticated:
            return

        print("  Authenticating...", end=' ', flush=True)
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
            print("FAILED", file=sys.stderr)
            raise SyncError(f"  Authentication request failed: {e}", EXIT_AUTH_ERROR) from e

        if response.status_code in (301, 302, 303, 307, 308):
            print("FAILED", file=sys.stderr)
            raise SyncError(
                "  Auth redirected — check client_id/client_secret.\n"
                "  Check: Administration → Account Management → "
                "Settings → Application Tokens",
                EXIT_AUTH_ERROR,
            )

        try:
            response.raise_for_status()
            token = response.json().get('access_token')
        except (requests.exceptions.HTTPError, ValueError) as e:
            print("FAILED", file=sys.stderr)
            raise SyncError(f"  {e}", EXIT_AUTH_ERROR) from e

        if not token:
            print("FAILED", file=sys.stderr)
            # Truncate to avoid leaking sensitive fields the server may return
            body_preview = response.text.strip()[:200]
            raise SyncError(
                f"  No access_token in response: {body_preview}",
                EXIT_AUTH_ERROR,
            )

        print("OK")
        self._session.headers.update({'Authorization': f'Bearer {token}'})
        self._authenticated = True

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
        alias = alias.strip('-_')
        return alias[:255]

    # ------------------------------------------------------------------
    # Platform hooks
    # ------------------------------------------------------------------

    def fetch_known_macs(self) -> set | None:
        """Return MAC addresses of devices that have connected to a Ruckus AP."""
        self._authenticate()
        url       = f'{self._api_base}/clients'
        macs: set[str] = set()
        page      = 0
        page_size = 100

        print("  Fetching WiFi clients...", end=' ', flush=True)
        while True:
            try:
                response = _request_with_retry(
                    lambda: self._session.get(
                        url, params={'page': page, 'size': page_size},
                        timeout=_RUCKUS_REQUEST_TIMEOUT,
                    )
                )
                response.raise_for_status()
                data = response.json()
            except (requests.exceptions.RequestException, ValueError) as e:
                print("FAILED", file=sys.stderr)
                raise SyncError(f"  {e}", EXIT_API_ERROR) from e

            if not isinstance(data, list):
                print("FAILED", file=sys.stderr)
                raise SyncError(
                    f"  /clients returned {type(data).__name__} instead of a list.",
                    EXIT_API_ERROR,
                )

            for client in data:
                try:
                    mac = client.get('mac', '')
                except AttributeError:
                    continue
                if isinstance(mac, str) and mac:
                    macs.add(mac.lower())

            if len(data) < page_size:
                break
            page += 1

        print(f"{len(macs)} found.")
        return macs

    def fetch_existing_names(self) -> dict[str, str]:
        """Fetch all current client aliases for skip-if-unchanged detection."""
        self._authenticate()
        url     = f'{self._api_base}/clients/aliases/query'
        aliases: dict[str, str] = {}
        page      = 0
        page_size = 100

        print("  Fetching existing aliases...", end=' ', flush=True)
        while True:
            try:
                response = _request_with_retry(
                    lambda: self._session.post(
                        url, json={},
                        headers={'Content-Type': 'application/json'},
                        params={'page': page, 'size': page_size},
                        timeout=_RUCKUS_REQUEST_TIMEOUT,
                    )
                )
                response.raise_for_status()
                data = response.json()
            except (requests.exceptions.RequestException, ValueError) as e:
                print("FAILED", file=sys.stderr)
                raise SyncError(f"  {e}", EXIT_API_ERROR) from e

            if not isinstance(data, dict):
                print("FAILED", file=sys.stderr)
                raise SyncError(
                    f"  /clients/aliases/query returned "
                    f"{type(data).__name__} instead of a dict.",
                    EXIT_API_ERROR,
                )

            for client in data.get('content', []):
                try:
                    mac   = client.get('macAddress', '')
                    alias = client.get('alias', '')
                except AttributeError:
                    continue
                if isinstance(mac, str) and mac:
                    aliases[mac.lower()] = alias if isinstance(alias, str) else ''

            if data.get('last', True):
                break
            page += 1

        print(f"{len(aliases)} found.")
        return aliases

    def set_device_name(self, mac: str, name: str) -> str:
        """Set a client alias in Ruckus One. ``name`` is already sanitized."""
        self._authenticate()
        url     = f'{self._api_base}/clients/aliases/{mac}'
        # deviceType must be 'WIFI' — Ruckus One aliases are WiFi-only;
        # wired clients are not manageable via this endpoint.
        payload = {'alias': name, 'macAddress': mac, 'deviceType': 'WIFI'}

        try:
            response = _request_with_retry(
                lambda: self._session.put(
                    url, json=payload,
                    headers={'Content-Type': 'application/json'},
                    timeout=_RUCKUS_REQUEST_TIMEOUT,
                )
            )
            if response.status_code == 202:
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


def build_platforms(
    config: configparser.SectionProxy,
    selected: list[str],
    dry_run: bool,
    quiet: bool,
) -> list[Platform]:
    """Instantiate and return the requested platform objects."""
    return [PLATFORM_REGISTRY[name](config, dry_run, quiet) for name in selected]


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
  omada    TP-Link Omada  (requires tplink-omada-api CLI)
  ruckus   Ruckus One     (requires API credentials in secrets.conf)

examples:
  python Firewalla-sync.py
  python Firewalla-sync.py --platform ruckus
  python Firewalla-sync.py --dry-run
  python Firewalla-sync.py --platform omada ruckus --dry-run
  python Firewalla-sync.py --quiet

exit codes:
  0  success
  1  configuration error (missing or invalid config)
  2  authentication failure
  3  network or API error
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
        help='Suppress per-device NOT FOUND output; show only summaries.',
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    args = parse_args()
    config = load_config()
    validate_config(config, args.platform)

    try:
        print("Fetching Firewalla devices...", end=' ', flush=True)
        devices = fetch_firewalla_devices(
            config['FIREWALLA_API_TOKEN'],
            config['FIREWALLA_MSP_ID'],
        )
        print(f"{len(devices)} found.")

        if args.dry_run:
            print(f"{YELLOW}Dry-run mode — no changes will be made.{RESET}")

        for platform in build_platforms(config, args.platform, args.dry_run, args.quiet):
            platform.sync(devices)
    except SyncError as e:
        print(str(e), file=sys.stderr)
        sys.exit(e.exit_code)
    except Exception as e:
        print(f"Error: unexpected failure: {e}", file=sys.stderr)
        sys.exit(EXIT_API_ERROR)

    print("\nDone.")


if __name__ == '__main__':
    main()
