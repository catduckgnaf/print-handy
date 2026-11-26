# OctoPrint 2.0.1 contract audit

Audit scope: Print Handy's read-only status path and the OctoPrint REST calls made by `print_handy/printers/octoprint/client.py` and `adapter.py`, against the upstream `2.0.1` tag (`012a92d7f5bffa1c2c059264968283fc578ca985`). This is a REST contract audit, not a printer or serial-hardware test.

## Findings

- **Connection credential disclosure:** OctoPrint 2.0.1 release notes say `GET /api/connection` redacts connection parameters when the caller lacks `CONNECTION` permission. The tagged implementation in `src/octoprint/server/api/connection.py` confirms that it returns `parameters={}` unless `Permissions.CONNECTION.can()` is true. This endpoint requires `STATUS`, so a STATUS-only account can otherwise retrieve connection state but no longer receives connector parameters. The legacy, unversioned response shape is still available and includes serial `port` and `baudrate`; Print Handy does not call `/api/connection` at all. It reads `/api/printer` and `/api/job` for status and sends commands only through its existing printer API paths. Thus no adapter regression or credential disclosure is present in this code path. The offline response fixture records the sanitized 2.0.1 response shape; it is illustrative contract evidence, not a live server capture.
- **Optional firmware identity:** The 2.0.1 release notes report a serial communication loop crash when `M115` has no `FIRMWARE_NAME` or `NAME`, fixed in this release. The tagged serial connector now defaults the parsed firmware name to an empty string and only runs firmware identity handling when a name is present (`src/octoprint/plugins/serial_connector/serial_comm.py`, around lines 2960–2981). This is upstream serial behavior. Print Handy does not retrieve firmware identity through OctoPrint's REST adapter and does not require it for printer/job status. An offline test pins that absent identity does not invalidate the REST status normalization. Whether particular firmware responds to `M115` with no identity, and its behavior through third-party serial connectors, remains unverified without those physical/connector combinations.
- **Versioned API:** OctoPrint 2.0.0 introduced `X-OctoPrint-Api-Version`; absent header preserves the legacy API behavior, while selecting 2.0.0+ changes response shapes (including `/api/version` and `/api/connection`). The client currently omits this header. The current calls do not consume either changed endpoint, so changing API version negotiation is not required for the audited behavior. No version header behavior was exercised against a live server.
- **API credentials:** OctoPrint 2.0.1 keeps `X-Api-Key` and Bearer authorization supported; documentation deprecates the global API key for removal in 2.1.0. Print Handy sends its configured key in `X-Api-Key` and does not put it in a URL. The documentation recommends user or app/user keys. No credential generation/revocation flow is implemented by this adapter, and none was tested.

## Baseline decision

The OctoPrint REST contracts used by Print Handy were checked against 2.0.1. The release adds the fixes above without changing the adapter's existing endpoints or response assumptions. Keep the established adapter behavior, add version-specific offline contract coverage, and record 2.0.1 as the audited baseline. This does not certify any physical printer, firmware, plugin connector, or real-server permission setup.

## Primary sources

- [OctoPrint 2.0.1 release notes](https://github.com/OctoPrint/OctoPrint/releases/tag/2.0.1)
- [OctoPrint 2.0.1 REST API: general authorization and versioning](https://docs.octoprint.org/en/2.0.1/api/general.html)
- [OctoPrint 2.0.1 REST API: connection handling](https://docs.octoprint.org/en/2.0.1/api/connection.html)
- [OctoPrint 2.0.1 REST API: version information](https://docs.octoprint.org/en/2.0.1/api/version.html)
- [OctoPrint 2.0.1 REST API: printer operations](https://docs.octoprint.org/en/2.0.1/api/printer.html)
- [OctoPrint 2.0.1 M115 firmware information hook](https://docs.octoprint.org/en/2.0.1/plugins/hooks.html)
- Upstream source tag: [`2.0.1`](https://github.com/OctoPrint/OctoPrint/tree/2.0.1), commit `012a92d7f5bffa1c2c059264968283fc578ca985`; `src/octoprint/server/api/connection.py` and `src/octoprint/plugins/serial_connector/serial_comm.py`.
