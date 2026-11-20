# Print Handy 2.3.6 (Draft / Unreleased)

Status: Draft / Unreleased. These notes describe changes merged to `main`; this is not a published release and has no release date or version tag.

## From “I found a model” to “that print cost me…”

Print Handy is built around the whole printing day, not a slicer window. Drop a model link or STL/3MF into Discord, Telegram, Matrix or the Web UI; answer the guided questions about material, copies and fit; then review the slice and use the usual explicit print confirmation. You can ask for supported changes in ordinary language instead of memorizing commands. When you do need the exact form, slash commands remain in the [command reference](../docs/PROJECT-REFERENCE.md).

This release makes that journey easier to steer and easier to understand afterward:

- **Choose the plate you meant.** Include `print plate 2` with a multi-plate model link, or say `print only the second part` when the part maps unambiguously to one authored plate. A same-channel reply to a Discord source message can carry your selection too. Ambiguous or unavailable choices do not silently become a different plate. (#1704, #1710)
- **Continue with the right project.** `/print-next <plan id>` can ask which remaining plate to print, and `/resume-model` can ask which archived source project to resume. Plain-language part selection can narrow the choice. These resume or choose work; they do not bypass the normal print confirmation. (#1706, #1711)
- **See what a print costs.** The calculator prices supported inputs using your configured settings and spool information. Completed print costs are frozen into records for the Data page, chat questions and Pasta Wrapped, so changing a spool price later does not rewrite history. See [Cost calculator](../docs/COST.md) and [Web UI](../docs/WEBUI.md). (#1632, #1640, #1647, #1657)
- **Share the relevant cost with HelixScreen.** Print Handy publishes the filament-plus-electricity estimate and actual to Moonraker's `helix-screen` database namespace using the v1 contract: key `print_cost.<encoded filename>`, with file revision, currency and separate estimate/actual blocks. It excludes labor, wear and other pricing lines. Publishing is best-effort; it does not hold up a print. The integration implements the contract agreed with [Preston Brown and the HelixScreen project](https://github.com/prestonbrown/helixscreen/issues/2132#issuecomment-6999700894). The matching HelixScreen reader is tracked in that project's [issue #2132](https://github.com/prestonbrown/helixscreen/issues/2132); see [the protocol details](../docs/COST.md#sharing-cost-with-helixscreen). (#1652)
- **Find the year in your prints.** The Web UI's grouped pages bring together printer status, designs, spools, cost and Data; Pasta Wrapped turns saved records into a year-in-review, with per-user and per-printer views. (#1642, #1657)
- **Get a more useful firmware digest.** Eligible Moonraker component updates arrive grouped by printer. A seven-day defer postpones the digest only; it does not install anything. Update actions still use their explicit confirmation and existing eligibility, idle and verification checks. Bundled updates are hidden by default; showing them is informational, not permission to install them. (#1705, #1707)

And the personality remains: local-corpus roasts, Pasta Cam's spaghetti watch, and non-AI status quips. Pasta Wrapped's default soundtrack includes “Pole Position Nights,” written by **Preston Brown** and provided through the **HelixScreen project**; see the [sound credits](../print_handy/data/audio/CREDITS.md) and its license/source details. Roasts are optional and local text; camera-analysis features have separate settings and may use a configured vision endpoint. (#1654, #1672, #1673, #1674)

## More useful details

- Cross-slicer 3MF intake translates a validated subset of foreign settings while the destination printer profile remains authoritative. The conversion report tells you what was preserved, translated, replaced, dropped or left unknown. See [Cross-slicer 3MF](../docs/CROSS-SLICER-3MF.md). (#1578, #1621)
- The cost calculator handles cost-only historical records, hours-only manual estimates, attached-file estimates, files dropped into chat, and binary ChiTuBox `.cxdlpv4` resin containers. (#1663, #1668, #1693, #1694)
- Slicer and project handling received fixes for Cura stack/instance selection, translated foreign-project settings, painted-fit tool maps and purge plans, safe package reuse, and ZMod upload slot choices. (#1653, #1665, #1685, #1686, #1689, #1670)
- Firmware release notices no longer imply an update is available when the installed version cannot be compared. (#1707)

## Upgrade and compatibility

No mandatory configuration migration is identified in the changes summarized here. `[firmware] hide_bundled_updates` defaults to `true`; setting it to `false` changes report visibility only, not update eligibility.

Hardware-specific validation is tracked in the [live-proof checklist #1082](https://github.com/catduckgnaf/print-handy/issues/1082). Consult that checklist for its open items; this document does not claim a hardware check was performed.

Merged PRs covered by this release range: #1566, #1567, #1568, #1569, #1572, #1573, #1574, #1575, #1576, #1577, #1578, #1580, #1581, #1582, #1591, #1594, #1597, #1602, #1604, #1605, #1606, #1607, #1609, #1611, #1613, #1614, #1618, #1619, #1620, #1621, #1628, #1629, #1630, #1631, #1632, #1633, #1635, #1639, #1640, #1641, #1642, #1644, #1645, #1646, #1649, #1650, #1652, #1653, #1654, #1655, #1656, #1657, #1658, #1659, #1662, #1663, #1664, #1665, #1666, #1667, #1668, #1669, #1670, #1671, #1673, #1674, #1685, #1686, #1688, #1689, #1690, #1691, #1692, #1693, #1694, #1695, #1696, #1698, #1699, #1701, #1704, #1705, #1706, #1707, #1708, #1709, #1710, #1711, #1712, #1713, #1714, #1716, #1722, #1723, #1724, #1725, #1727, #1728, #1730.

## References

- [Cross-slicer 3MF #1578](https://github.com/catduckgnaf/print-handy/pull/1578) · [cost calculator #1632](https://github.com/catduckgnaf/print-handy/pull/1632) · [grouped Web UI #1642](https://github.com/catduckgnaf/print-handy/pull/1642) · [Data and Pasta Wrapped #1657](https://github.com/catduckgnaf/print-handy/pull/1657)
- Plate/project selection: [#1704](https://github.com/catduckgnaf/print-handy/pull/1704), [#1706](https://github.com/catduckgnaf/print-handy/pull/1706), [#1710](https://github.com/catduckgnaf/print-handy/pull/1710), [#1711](https://github.com/catduckgnaf/print-handy/pull/1711)
- Firmware notices: [#1705](https://github.com/catduckgnaf/print-handy/pull/1705), [#1707](https://github.com/catduckgnaf/print-handy/pull/1707)
