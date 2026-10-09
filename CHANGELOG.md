# Changelog

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
