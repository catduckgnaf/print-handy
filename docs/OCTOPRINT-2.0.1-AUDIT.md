# OctoPrint 2.0.1 contract audit

Audit scope: Print Handy's read-only status path and the OctoPrint REST calls made by `print_handy/printers/octoprint/client.py` and `adapter.py`, against the upstream `2.0.1` tag (`012a92d7f5bffa1c2c059264968283fc578ca985`). This is a REST contract audit, not a printer or serial-hardware test.

## Findings

- **Connection credential disclosure:** `GET /api/connection` requires only `STATUS` permission in both response shapes (`src/octoprint/server/api/connection.py` at the tag). The two shapes differ in exposure:
  - *2.0.0+ shape* (client sends `X-OctoPrint-Api-Version` 2.0.0 or later): `current.parameters` is `{}` unless the caller also holds `CONNECTION` permission. A STATUS-only key gets connector name and state but no connector parameters (release note for 2.0.1; code: `parameters=connection_state if Permissions.CONNECTION.can() else {}`).
  - *Legacy unversioned shape* (no version header, which is what Print Handy sends if it ever called the endpoint): `current.port` and `current.baudrate` are returned with no `CONNECTION` check, so a STATUS-only key still sees the serial port and baud rate. This is not credential material, but it is connection detail that 2.0.1 does not redact in this shape.

  Print Handy does not call `/api/connection` (or `/api/version`) from any client method; it reads `/api/printer` and `/api/job` for status and uses its existing printer API paths for commands. Offline tests pin that no client method requests those endpoints and that the adapter source never names `/api/connection`. No adapter regression or connection disclosure exists in this code path. The fixtures `connection-status-no-connection-permission.json` and `connection-status-legacy-unversioned.json` record the sanitized shapes from the tagged models; they are illustrative contract evidence, not live server captures.
- **Optional firmware identity:** The 2.0.1 release notes report a serial communication loop crash when `M115` has no `FIRMWARE_NAME` or `NAME`, fixed in this release. The tagged serial connector now defaults the parsed firmware name to an empty string and only runs firmware identity handling when a name is present (`src/octoprint/plugins/serial_connector/serial_comm.py`, around lines 2960–2981). This is upstream serial behavior. Print Handy does not retrieve firmware identity through OctoPrint's REST adapter and does not require it for printer/job status. An offline test pins that a REST snapshot without any firmware field still normalizes and that the adapter emits no identity. It does not exercise `M115` itself, which stays serial-side. Whether particular firmware responds to `M115` with no identity, and its behavior through third-party serial connectors, remains unverified without those physical/connector combinations.
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
