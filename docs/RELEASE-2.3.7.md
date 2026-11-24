# Print Handy 2.3.7 (Draft / Unreleased)

Status: Draft / Unreleased. These notes describe changes merged to `main`; this is not a published release and has no release date or version tag.

One Docker image for amd64 + arm64: ghcr.io/catduckgnaf/print-handy:2.3.7, also tagged latest.

Big models stop making you wait in silence, you can talk to a job while it prepares, PETG finally slices like PETG, and your printer screen and Print Handy argue less about what is in slot 3. This release also carries the spring cleaning that was planned for 2.4.0. It ships now, so **read the upgrade section before you pull the image.**

## Upgrade first: what breaks for existing installs

2.3.7 removes the compatibility paths 2.3.6 still kept (#1817, #1849). Nothing changes silently: an old setting either stops startup with a message that names it and its replacement, or loads with a warning that names it. The one exception is the old short environment variable names, which are simply no longer read (item 2). Stop Print Handy and back up its data folder before you upgrade, then go through this list. On a native install, `python -m print_handy.doctor` names any state still in an old place and prints the moves; on Compose, `docker compose exec combined python3 -m print_handy.doctor` runs it once the container is up. The full list lives in [Removed in 2.3.7](SETTINGS.md#removed-in-237).

1. **Settings files are YAML only.** `config/local.toml`, `home.toml`, `docker.toml` and any `.toml` file named by `PRINT_HANDY_CONFIG` are refused at startup by name; nothing is translated for you any more. Convert the file to YAML with the same keys (`config/local.yaml`, or point `PRINT_HANDY_CONFIG` at a `.yaml` file). Docker ships only `/app/config/docker.yaml`; a YAML file you mount replaces that layer, so copy `config/docker.yaml` first if you want its defaults. See [YAML configuration](CONFIGURATION-YAML.md). (#1854)
2. **Only `PRINT_HANDY__SECTION__KEY` environment variables are settings.** The old short names (`MOONRAKER_URL`, `SPOOLMAN_URL`, `PRINT_DISCORD_CHAT_ID`, `PRINT_TIMELAPSE_*`, `MAINTENANCE_*`, `FFMPEG_BIN`, `PIPER_BIN`, `PIPER_MODEL`, `PRINT_HANDY_ARCHIVE`, `PRINT_RECORDS_VAULT` and the rest of the old table) are no longer read, **and nothing warns about them**. Rename every one you set, for example `MOONRAKER_URL` becomes `PRINT_HANDY__SERVICES__MOONRAKER_URL`. (#1834)
3. **Renamed keys are refused instead of read under the old name.** `paths.hermes_home`, `webui.secure_cookie`, `printer.ifs_slots`, `discord.mention_starter_on_complete` and `timelapse.wrapped_ai_video` stop the load, in a file or as `PRINT_HANDY__...`, and the message names the current key (for example `discord.mention_starter`). (#1834)
4. **AI settings: one endpoint, `[llm]`, and nothing else.** Every AI feature (Web Chat, Design, AI fix, camera checks, spool label and purchase photos) uses the one `[llm]` endpoint: `llm.chat_url`, `llm.model` and `llm.api_key_env`, with optional per-purpose model names. Remove the retired keys: the whole `[agent]` section, `design.chat_url`, `design.api_key_env`, `services.vision_chat_url`, `services.openviking_url`, `vision.model`, `discord.chat_llm_enabled`, and the `[ai.<tier>]` keys `chat_url`, `fallback_model`, `fallback_url`, `api_key_env`, `fallback_api_key_env` and `timeout_s`. A credential-shaped key (`api_key_env`) and any of the `[ai.<tier>]` keys stop the load; the others load with a warning. The environment variables `PRINT_VISION_URL`, `PRINT_FUZZY_URL`, `CLIPROXY_API_KEY` and `AGENT_API_KEY` are not read. A failed model call reports its error instead of retrying on a fallback model, and the semantic `/print-lookup` that went through `PRINT_FUZZY_URL` is gone (local matching stays). Earlier in this range #1780 migrated matching old keys into `[llm]` on load; #1834 removed that again, so 2.3.7 migrates nothing. See [Retired settings](SETTINGS.md#retired-settings), including the Design-only migration example. (#1741, #1754, #1780, #1834)
   - **Where your images go:** camera frames, label photos and purchase photos now go to the `[llm]` endpoint. If an old config sent them to a local vision endpoint while `[llm]` points at a hosted provider, they would now reach the hosted provider. To keep them local, point `llm.chat_url` at the local endpoint, or set `vision.ai_enabled: false`. With no `[llm]` configured, nothing is sent. (#1754)
5. **`[ai] allowed_users` is one string** of IDs separated by commas or spaces. A YAML list is refused. (#1834)
6. **`[bot] mode` is gone**: the bot always runs itself. An old `mode:` line or `PRINT_HANDY__BOT__MODE` is an unknown setting and is ignored with a warning. (#1834)
7. **State in old places is no longer read.**
   - **Docker and Unraid, the pre-2.2.5 `hermes` folder:** an install whose state still sits only in `hermes`, next to an empty `state` folder, refuses to start with one line saying so, instead of coming up empty. Stop the container, delete the empty `state` folder (or a `state` symlink an older release made), then rename `hermes` to `state` (`${PRINT_HANDY_DATA:-./data}/hermes` on Compose, the `hermes` folder under the Print Handy appdata folder on Unraid). Delete `state` first, because `mv hermes state` onto an existing folder moves `hermes` inside it. If you set `PRINT_HANDY__PATHS__STATE_HOME` or `logging.file` to a `/data/hermes` path, change it to `/data/state`. An install that already keeps its state in a configured state folder is not refused because old files linger in `hermes`. See [Old `hermes` state folder](DOCKER.md#old-hermes-state-folder-237). (#1836)
   - **Native installs, `~/.hermes/profiles/3dprinting`:** with `paths.state_home` unset, the state folder is `~/.local/share/print-handy`; the old fallback is gone. The doctor's "legacy state folder" row fails while the bot token, hardware record or launch database exist only in the old folder and prints the exact moves. Setting `paths.state_home` to the old folder also works. (#1834)
   - **Root-level state files:** each printer's hardware record, maintenance record and launch database live in `state/printers/<id>/`. A file still at the old root location is refused at startup with the exact `mkdir -p … && mv …` that moves it (SQLite `-wal`/`-shm` files included), so the default printer never starts empty; the doctor's "pre-2.4 state files" row prints the same moves. Stop Print Handy before moving files; in Docker, run the moves on the host under `data/state/` and keep the folder owned by uid 1000. See [SETTINGS.md](SETTINGS.md#printers-list). (#1837)
8. **Re-slice saved jobs from older versions before you print them.** G-code that carries the old `; hermes_tool_slots = ` map line is refused with a reslice instruction, and files sliced with the retired `Hermes slot <n> <material>` filament profiles must be resliced too. Nothing upgrades an old file in place. (#1827, #1830)
9. **Home Assistant REST sensor on the Moonraker job card:** the card is now written under namespace `print_handy`, key `current_job`. A hand-written REST sensor must request `/server/database/item?namespace=print_handy&key=current_job`. The `ha-print-handy` integration does not read this key and needs nothing. (#1830, #1851)
10. **Native installs from before 2.3.0, and custom logging levels:** a systemd unit that still runs `python -m print_handy.hermes.<name>` no longer starts; re-run `scripts/install-standalone.sh` to rewrite the units. A `[logging] levels` key for `print_handy.core.model_job` or `print_handy.core.prepared_intake` no longer matches anything: name the module itself, for example `print_handy.launch.print_now`, `print_handy.jobs.slice` or `print_handy.intake.worker`. Docker installs are unaffected. (#1835)
11. **OrcaSlicer 2.5.2.** The image bundles OrcaSlicer 2.5.2 (was 2.5.1). A native install should install the 2.5.2 Flatpak ([install guide](INSTALL.md)). The retired Flatpak app ID `io.github.softfever.OrcaSlicer` is refused in `[orca].command` and `ORCA_FLATPAK_APP_ID` with the current ID named; use `com.orcaslicer.OrcaSlicer`. (#1851, #1875)

Things you will notice, with nothing to do:

- The chat Ready card no longer shows an estimated cost line; set `cost.ready_card_estimate: true` to bring it back. Export editable 3MF moved off the chat Ready menu and stays on the Web UI job page. (#1813)
- The Web UI is dark only, with no theme toggle; an old theme cookie is ignored. Settings and Notifications sit in the header, and the Designs tab is now Chat / Design. (#1739, #1753)
- Discord is a pure bot: it makes no model calls of its own. (#1741)
- Models are no longer refused for their size alone. A download still stops, and the partial file is removed, when keeping it would leave less than 512 MiB free. (#1833, #1852)
- An old Discord button that outlived a restart or a timeout says nothing was approved or started, is removed, and asks you to send the model again. Approvals never carry over a restart. (#1773)
- The camera watch's local failure detector model ships inside the image, so it works offline from the first start, and Print Handy no longer contacts obico.io. A source install downloads the model once from Print Handy's own model repository. (#1848, #1856)

Rollback: use the previous image digest with the same data paths ([Rollback](DOCKER.md#rollback)). Going back to 2.3.6 after making the moves above has not been tested; keep the backup you made before upgrading.

## What you can do

### Talk to a job while it prepares

- Type `cancel` or `stop` in the channel to stop a job before anything is uploaded or started, `status` to hear which step it is on and for how long, and anything else gets a reply about the job instead of the generic help. Every long step before upload (downloading, checking, combining files, inspection, slicing) shows a Stop button, including the file-combining step that used to be stoppable only by killing the worker. After upload, the existing start gates and printer cancel apply as before. (#1793)
- A slow step shows how long it has been running in the same message as its Stop button ("Slicing… 18 s") and says "done" with the total when it finishes, updated in place on Discord, Telegram and Matrix. (#1799, #1818, #1820)
- Starting a print on Discord keeps one message that moves through printer preparation, upload, waiting for confirmation and the confirmed start, then is replaced by the actual outcome. An OrcaSlicer toolpath conflict names the parts to inspect and says what to change before slicing again, instead of offering an AI repair. (#1873)
- A big source says so right after the download ("These files are big (56 MB of mesh, about 1.1 million triangles); preparing them takes a few minutes.") instead of going quiet. (#1800)
- A model page whose files are versions of one part (`SnackTongs` and `SnackTongs_FlatProfile`, `_alt`, `_low_profile`, `_remix`) asks which version to print instead of printing all of them together. (#1801)

### Big models get to the first question much sooner

- On a 56 MB, 1.1 million triangle two-file model, the checks before the first question that it did not use went from about 103 s to 6 s, combining the files from about 35 s to 3 s with the native helper the Docker image ships, and the combined-model check from about 56 s to 22 s (measured on test machines). Installs without the native helper combine the same pair in about 13 s instead of about 2 minutes. The printed geometry is unchanged. (#1790: #1792, #1802, #1808, #1829)
- Painted 3MF uploads are inventoried about 3x faster on the paint census, with identical colors, palettes and questions. (#1738)
- Discord stays responsive while Print Handy checks support coverage on a large sliced file, and the coverage check reads the plate G-code once. (#1772, #1842)
- Opt in to slicing the with- and without-supports previews at the same time on hosts with 4 or more CPUs (`slicer.parallel_preview`, off by default, never more than two Orca runs at once). (#1823)
- A standalone 3MF under the geometry budget is no longer refused by fixed XML or member size ceilings, and refusals name the file and the measured geometry. (#1852)

### Spools and slots that match your printer screen

- On a printer with a touch screen, such as the AD5X with HelixScreen, Print Handy asks which spool is loaded when the screen and Print Handy disagree about a slot, or when a slot was reloaded with nobody saying which spool went in. The question is advisory: it appears only for slots the print uses, has a Skip button, and never holds or cancels the print. The HelixScreen conflict notice has Same spool, Existing spool and New spool buttons. (#1788: #1794, #1803, #1805, #1809)
- Say "I swapped slot 3 for Coffee" and the guided spool change opens with matching spools first; the reply says whether the screen's lane link to the new spool held. (#1810)
- Ask "which spool is in slot two" in plain words and get the printer, the recorded spool and its remaining weight. (#1857, #1878)
- Spool labels read the same everywhere, for example "Black Matte, PETG HS". (#1788 phase B: #1806, #1814, #1815, #1821)
- Slot verification no longer reports a mismatch from the previous print's channel while ZMod is still switching tools, and says plainly when its readings stay inconclusive after a missing baseline. (#1828, #1843)

### Slice PETG like PETG

- Non-PLA filaments slice with the printer maker's own Orca profile for that filament and nozzle, so high-speed PETG on the AD5X uses Flashforge's HS PETG flow, fan and temperature settings instead of PLA's. Sliced files name the profile they used. Print Handy now carries a database of every OrcaSlicer vendor profile. (#1816: #1819, #1827, #1831; #1832: #1841, #1845, #1877)
- Tell Print Handy which build plate each printer has (`printer.build_plate`: `smooth_pei`, `textured_pei`, `smooth_pc` or `cool_plate`, also in the printer editor). The slicer gets the right plate, and Print Handy warns before a print when the material can damage it, such as PETG on smooth PEI. Unset changes nothing. (#1798, #1812)
- Projects you laid out yourself on one plate keep that layout for a multicolor slice when it already meets spacing, edge, exclusion and prime tower checks; otherwise the plate is repacked as before. (#1862, #1874)
- Color selection shows the designer's palette separately from suggested and final colors, and eight-digit colors such as `#F9E2B7FF` keep their exact value with a readable name. (#1872)

### Fix a model without posting it again

- When supports cannot reach a low gap, Extend underside to plate and Flatten (sink) to plate lead the Ready menu and appear at the post-slice warning. A repair re-slices a copy and runs every check again; it never prints by itself. Ready can show whether the with-supports preview covered the target regions, and an unchanged slice reuses that check. (#1775, #1795)
- Early preview: `/fix` with a model, "fix this" sent with a model, or "Work on this with AI" after a failed slice opens an AI fix session that works on a copy with the print repair tools and needs a configured `[llm]` endpoint. `accept` sends the repaired copy through the normal intake and confirmation; a step that changed nothing is reported as no change. The rest of this feature is still open on #1778. (#1777: #1807; #1778: #1840)

### One AI endpoint, configured once

- Web Chat, Design, AI fix and camera checks share the `[llm]` endpoint, documented in the README and the standalone guide, with the default model the Design migration needs. A setup whose old keys do not agree is named in the Doctor and on the Status page ("Attention needed"). (#1731: #1740, #1741, #1743, #1746, #1754, #1780)

### Web UI

- The Chat / Design page keeps a refused Design answer in view with its reason, and the Firmware page has a shorter intro with commands in a help section and a Sources table that scrolls on phones. Print cost puts file auto-fill first and tucks less-used inputs under Advanced. Phone controls are at least 44 px. (#1737: #1739, #1744; #1785, #1796, #1804)
- The Data page explains how its success rate is counted, shows prints with no recorded outcome as their own part, and says how many prints recorded a filament weight. (#1784)

## Fixes

- Prints whose completions were recorded under timestamp names were invisible to Data and Pasta Wrapped (Data showed 6 finished out of about 60 resolved). Completions now finalize their numbered start, and Data and Wrapped read every record. (#1758, #1774)
- Single-color ZMod jobs launched from Print Handy could start with the wrong spool; the selected slot is kept. (#1781)
- On the AD5X (ZMod IFS), a filament jam was announced as a runout with a spool question. It is now a jam, a jam right after a resume says "Still jammed", the pause texts are shorter, and when the job's slot and the printer's channel disagree the alert shows both instead of naming one. (#1787, #1791, #1811)
- A print's roast could reach the printer screen and the Home Assistant last-roast sensor twice when the completion post was delivered again, and two prints finishing at once could send the sensor's discovery config twice. Both happen once now. (#1797)
- A printer whose hardware record predates the slot-map rename kept losing its loaded spools after moving into the per-printer folder; a partly failed Spoolman seed no longer leaves half its claims behind; unloading the last spool on the screen stays unloaded. (#1837)
- A pending completion, roast subject or timelapse key left in an old single field was treated as missing after upgrade and never retried; it is read and moved into the printer's own slot. (#1838)
- Slot colors, G-code verification, slot remaps and panic recovery always use the intended printer instead of deriving the default one. (#1853, #1855)
- Pasted login tokens and Web UI passwords are deleted from chat again where the platform allows it, and the design view links to sliced files again. (#1839)
- Slicing a non-PLA spool whose Spoolman filament names a vendor failed with an internal error, and a vendor profile with a non-numeric flow ratio stopped the slice. Both slice now. (#1841, #1845)
- On the shipped OrcaSlicer 2.5.2 profiles, templates for the four Prusa models were refused; they generate again. (#1871, #1875)
- The optional Bambu Studio fallback is skipped, with Orca's own conflict details kept, when its executable is not installed. (#1869)
- A Docker install that already keeps its state in a configured folder was refused at startup over leftover `hermes` files. (#1836)

## Technical details and contributors

- 2.4.0 hard-cut work shipped here (#1817, #1849): settings shims and renamed-key aliases removed (#1834), module shims removed (#1835), Docker `hermes` volume handling (#1836), one state layout (#1837), record shapes (#1838), code cleanups (#1839), the G-code tool map and job card namespace renamed into one `tool_map` module (#1830), the retired Flatpak ID (#1851), explicit printer-state routing and a documentation sweep (#1853), YAML-only settings (#1854), test hardware records under printer state roots (#1855), bound launch replay fixtures (#1876), and the docs and messages naming 2.3.7 (#1879, #1881).
- Docker: the bundled vision model directory is traversable for the runtime user (#1856); OrcaSlicer 2.5.2 pins for both architectures and the regenerated profile database (#1875).
- Release and CI: five discord-timing repeats (#1735), faster releases with notes coverage fill, light CI for bump PRs and one image build per release (#1736), untagged candidate digests documented (#1748), seeded Web UI layout audits in Chromium (#1783, #1786), daily firmware feed and a cached Rust toolchain (#1850), the 2.3.7 timing baselines (#1880), and the 2.3.7 release notes page (#1882).
- Unraid: the Community Applications template's upgrade text names 2.3.7 (catduckgnaf/catduck-unraid-templates#50).

## Verification and known limitations

- Automated: the offline suite, the mock-slicer timing gate and the real-OrcaSlicer jobs run in CI on the release commit; the 2.3.7 timing baselines were recorded from `main` run 45438378964 as new per-CPU profiles (mock leg on an AMD EPYC 9V74, real-OrcaSlicer leg on an Intel Xeon 6975P-C) (#1880).
- Not verified live: no printer, camera, hosted AI provider or real install was exercised for these notes. The live checks the range's PRs owe (ZMod slot launches, spool swaps on the screen, large real downloads, vendor-profile slices on a printer) are deferred on [release checklist #1870](https://github.com/catduckgnaf/print-handy/issues/1870) and tracked in [#1082](https://github.com/catduckgnaf/print-handy/issues/1082). Real-install timing was not measured for this release.
- The AI fix session (#1840) is an early preview; #1778 stays open.
- The real-OrcaSlicer timing leg reports one unexplained rise (witch-ghost, +0.33 s on one runner class since 2.3.4); it is reporting-only and recorded in [Performance regressions](history/PERFORMANCE-REGRESSIONS.md).

Merged PRs covered by this release range: #1735, #1736, #1738, #1739, #1740, #1741, #1743, #1744, #1746, #1748, #1753, #1754, #1772, #1773, #1774, #1775, #1780, #1781, #1783, #1784, #1785, #1786, #1791, #1792, #1793, #1794, #1795, #1796, #1797, #1798, #1799, #1800, #1801, #1802, #1803, #1804, #1805, #1806, #1807, #1808, #1809, #1810, #1811, #1812, #1813, #1814, #1815, #1818, #1819, #1820, #1821, #1823, #1827, #1828, #1829, #1830, #1831, #1833, #1834, #1835, #1836, #1837, #1838, #1839, #1840, #1841, #1842, #1843, #1845, #1848, #1850, #1851, #1852, #1853, #1854, #1855, #1856, #1869, #1872, #1873, #1874, #1875, #1876, #1877, #1878, #1879, #1880, #1881, #1882.

## References

- [Removed in 2.3.7](SETTINGS.md#removed-in-237) · [YAML configuration](CONFIGURATION-YAML.md) · [Docker](DOCKER.md) · [Install](INSTALL.md) · [Project reference](PROJECT-REFERENCE.md)
