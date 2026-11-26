## Audit boundary and version evidence

OpenCreator 0.0.7.5 and 0.0.7.6 were reviewed against 0.0.7.4. Their release notes report no MCU change: all three carry Kalico `7ce49713`. Version 0.0.7.5 changes the tunneled host runtime (hard-float Python, network time at boot, and Python 3.8 fallbacks); 0.0.7.6 changes the HelixScreen MIPS package and moves an existing stable updater entry to beta. Neither release note claims changes to Moonraker objects, AFC/ACE, tool routing, print-start synthesis, or nozzle temperature behavior.

The firmware baseline can therefore advance to 0.0.7.6 for those release notes, but not beyond it based on this audit. Upstream 0.0.7.7 was published 2026-11-25 and is outside this audit. It claims a Full Rollback fix; the audit does not claim rollback behavior or the latest release is verified. Re-audit before the baseline advances again.

ACE/AFC and edition distinctions: the existing contracts are unchanged. ACE Pro is not in these 0.0.7.x release notes (the existing page lists ACE Pro multiplexing as Planned upstream); AFC lanes remain the relevant released tool map. CFW uses the printer host as the Moonraker endpoint, while SBC edition uses the SBC endpoint and distinct updater component. The 0.0.7.5 release notes list no network-target change. HelixScreen is an add-on package, not a separate printer firmware contract.

## Upstream OpenCreator #19

The report describes an OpenCreator AFC runout, followed by the operator manually selecting a replacement tool. Its log names `C5_TOOL_SELECT T=2`; the author reports dock heating at 250 C and expected a lower-temperature staged handoff. It does not show a Print Handy-issued command or establish that Orca's 235 C profile target was passed through the manual UI action. Print Handy does not infer or command replacement-tool selection or target temperature from Spoolman metadata. Its OpenCreator runout buttons are intentionally non-dispatching until hardware validation. No Print Handy defect is established by this report. Avoid turning the anecdote into a universal claim about firmware behavior.

## Hardware-unverified

No physical OpenCreator printer was actuated or tested. The following remain unverified: actual CFW and SBC installs/updates, HelixScreen MIPS beta install/update, AFC/ACE runout failover and dock temperatures, and Print Handy runout alert presentation during an actual print. The upstream #19 behavior was read from its public issue report and logs, not independently reproduced.
