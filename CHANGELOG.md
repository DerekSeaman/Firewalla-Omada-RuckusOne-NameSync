# Changelog

## 1.3.1

### Changed

- Omada: list each updated client (`[UPDATED]` in green, with MAC and name),
  as Ruckus One does. `--quiet` hides these lines.
- A device whose name has no characters the platform accepts (e.g. an
  emoji-only name on Ruckus One) is now listed as `[SKIPPED]` instead of
  `[FAILED]`, and no longer makes every run exit with code `4`.
- Ruckus One: accented letters keep their base letter (`Küche` → `Kuche`,
  previously `Kche`) and tabs become hyphens. Affected aliases are re-pushed
  once.
- An empty device list from Firewalla is now an error (exit code `3`)
  instead of a silent success.
- Replies of `429`, `502`, `503` or `504` are retried up to twice, honouring
  `Retry-After`, instead of failing the device at once.
- A platform that stops answering is abandoned after three connection
  failures in a row (exit code `3`, remaining devices shown as "Not
  attempted") instead of timing out on every remaining device.
- Ruckus One: a token still rejected after re-authenticating ends that
  platform's run with one error instead of one failure per device.
- Omada: the script logs out when it finishes, and logs in again once if
  the session ends mid-run.
- Error messages name the platform, the HTTP status and the setting to
  check, and no longer print request URLs (the Ruckus One URL contains the
  tenant ID).
- Exit codes follow the cause: a rejected Firewalla token is now `2` (was
  `3`); an unreachable Omada controller or Ruckus One sign-in service is
  now `3` (was `2`).
- Firewalla entries that are ignored (no name, invalid MAC, unexpected
  format) are counted on an `Ignored …` line (hidden by `--quiet`).

### Fixed

- Omada `--dry-run` previewed updates for devices the controller doesn't
  know; they are now shown as `[NOT FOUND]`, as in a real run.
- Omada: the client list could stop after 100 clients if the controller
  returned smaller pages than requested or no row total, so later clients
  were re-pushed on every run.
- Ruckus One: the WiFi client list stopped at a page containing only
  already-seen clients, reporting clients on later pages as not found.
- Omada: a name such as `- Kitchen` was pushed with a leading space.
- A MAC address followed by a line break was accepted as valid.
- Output under a non-UTF-8 locale stopped the run with exit code `5`;
  unrepresentable characters are now written as escapes.
- Ruckus One: the alias list stopped after 100 aliases if the API omitted
  its "last page" flag, so later aliases were re-pushed on every run.
- Ruckus One: a client list whose rows have no readable MAC is now an error
  (exit code `3`) instead of reporting every device as not found.
- `secrets.conf` is read as UTF-8 regardless of locale; a file in another
  encoding gives a clear error instead of a traceback.
- Omada: null or empty controller ID, login token or site ID in the
  controller's replies now give a specific error. The login error no longer
  prints the response body.
- Omada `--dry-run` on a controller with no clients previewed updates
  instead of `[NOT FOUND]`.
- Line breaks and control characters inside a Firewalla device name are
  replaced by a space.
- `OMADA_SITE=` left blank now means `Default`; `OMADA_VERIFY_SSL` accepts
  `no`/`0`/`off` and rejects unknown values.

## 1.3.0

### Added

- Ruckus One: list each updated device (`[UPDATED]` in green, with MAC and alias)
  in addition to the `[NOT FOUND]` lines.
- Omada: skip devices whose name is already correct, as Ruckus One does.
- Exit code `4` when devices fail to update, and `5` for internal errors.
- Warning when `secrets.conf` is readable by other users; `NO_COLOR` support.

### Fixed

- A `%` in any `secrets.conf` value crashed the script at startup.
- One platform failing (e.g. Omada unreachable) stopped the other platforms
  from syncing.
- The script exited `0` even when every device failed to update.
- Unexpected API responses from Omada or Ruckus One aborted the whole run
  instead of failing a single device or platform.
- Ruckus One client lists could be cut short if the API returned smaller
  pages than requested; an API ignoring the page number could loop forever.
- Ruckus One: any `2xx` response now counts as success (not only `202`), and
  an expired token is renewed mid-run.
- MACs from Ruckus One are compared regardless of case or separator.
- The same MAC reported by several Firewalla boxes was pushed repeatedly
  with conflicting names.
- Non-text or blank Firewalla names aborted the run or were pushed as blank.
- `--quiet` now hides `[UPDATED]` lines; `--platform` ignores repeated names.
- Omada `--dry-run` now logs in, so it catches bad credentials or site names.
- Truncated Ruckus One aliases could end in `-` or `_`.
- Internal errors now print a traceback; output stays in order in cron logs.
- `pyproject.toml` version synced with the release tags (was still 1.0.0).

Earlier releases: see the `v1.0.0`, `v1.1.0` and `v1.2.0` git tags.
