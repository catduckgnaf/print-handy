# Print Handy 2.2.9: filament problems warn instead of block, multi-part projects you can see, a fit probe that knows your bed, and a Web UI you can leave running

One Docker image for amd64 + arm64: ghcr.io/catduckgnaf/print-handy:2.2.9, also tagged latest.

2.2.8 was a test tag that was never released from main. 2.2.9 is the release that follows 2.2.7, so these notes cover everything since 2.2.7. If you upgrade an existing install, read "Upgrade and compatibility" first.

## Encoder preference

Timelapse assembly defaults to CPU `libx264` and does not probe GPU encoders. Explicit `auto` still tries VAAPI → NVENC → QSV → CPU, and explicit hardware choices remain available. GPU encoding remains the default for live Discord streaming. Existing timelapse configurations that explicitly set `auto` retain that behavior. (#1282)


### Print when the filament data is imperfect
- Filament problems no longer block a print. A plate whose filament usage cannot be fully determined, a support filament outside the project palette, a slicer report that disagrees with the geometry, and a damaged usage claim in the source file now stop once with the warnings named. You can accept them and print. The acceptance is recorded on the job and covers one slicing run, so a reslice asks again. (#1190)
- The last three filament refusals work the same way: no validated slot mapping, a slot past the printer's slot count, and color choices never made. The warning names the slot the print will use, and accepting prints from that slot. Declining slices nothing. (#1213)
- Per-plate filament usage no longer under-counts. Each case either counts the filament the source really uses or says the usage is unknown, which is a warning you can accept. (#1203)
- A multi-plate job now gets filament warnings for all of its plates. Before, whole-source multi-plate jobs got none. (#1202)
- A foreign OrcaSlicer project that sends its walls, infill, prime tower or supports to a specific filament keeps that choice, remapped with the same slot mapping as the parts, instead of carrying a stale number into the renumbered file or dropping it without a word. (#1217)
- Modifier parts that print a region in another filament (a band, a logo) are no longer ignored. If one of your color choices already covers that filament, the region prints with it. If none does, you get a filament warning to accept instead of a silently one-color region. PrusaSlicer projects with color or tool changes by height now warn that those changes are not carried into the print, instead of claiming the plate needs nothing extra. (#1238)

### See and continue multi-part projects
- The Web UI has a **Projects** page: each multi-part project's components, plates with their aliases, required and printed quantities, every print attempt, and whether it is complete, printing, or can be resumed (with the chat command that continues it). (#1218)
- `/print-next` asks **Which plate is next?** on Telegram, Matrix and web chat too, not only Discord. `/print-again list` groups a project's prints under the project with each plate's alias. Reprinting a whole project starts a new run, so it no longer counts toward the original project. (#1218)

### Check that a part fits before you commit
- The fit probe keeps the printer's excluded bed areas, rejects an overheight part at the same strict limit slicing uses, and reserves every brim type. Tiny assembly overlaps no longer round to clearance. (#1137) (#1175)
- Multi-printer installs: the fit check uses each printer's own bed exclusion area. Before, every job used the default printer's, so a printer with no exclusion could be refused around a corner it does not have. Single-printer installs were never affected. (#1232)
- A failed fit probe now names the slicer failure instead of implying that no size fits. (#1165)
- Lay-flat is faster because it measures contact with the supporting plane instead of adding up parallel terraces, and it handles inside-out meshes. A 3MF part placed with a mirroring transform is no longer flattened inside out in the fit triangles or the STL handed to the slicer. (#1137) (#1175)
- Support detection no longer excuses a wide, shallow, faceted underside as a harmless bottom chamfer. The check is stricter, never looser. (#1137)
- Reorient remembers the rotated size, so "make it 50 mm tall" on a rod laid flat uses the laid-flat pose. Parts exported with cutters are centered on your printer's bed instead of a fixed point. (#1137)
- A reslice with supports now clears the earlier low-clearance acknowledgement, so the new file's clearance finding is asked again before anything prints. (#1174)

### Get answers faster
- Fewer slicer probes when finding the biggest fit, lazy vendor imports, working indicators that show up right away, a five-second shared `/api/status` snapshot, one verified G-code read in `print_now`, and faster pending web-chat updates. Decision gates and verification are unchanged. (#1165)
- Offline G-code verification reads a file once instead of about ten times, the chat stays responsive while a large painted model is being fitted, and the settings page stops re-parsing unchanged settings files. No verdict changes. (#1222)
- Slicing a painted model reads each model member once instead of more than ten times (#1217), and Discord color questions for large painted models analyze the model once per conversation instead of up to three times (#1238).
- Chat forgives hundreds more everyday typos: "epsecially", "acess", "pasword", "wensday", "acknowladge" and "comitted" all reach the word you meant. Real words that only look like typos, such as "planing", are left alone. (#1191)
- The slicing timing gate now also times a typed-message conversation and the print-next path, and its baseline bookkeeping is corrected. (#1188) (#1170)

### Design a part by talking to the bot
- Design makes sensible assumptions and says what it assumed, asks at most one question, checks its own render (size, one solid, on the bed, fits the printer) before showing anything, and explains in plain words what it made and how to change it. Undo works as a stack. (#1136) (#1209)
- Typed answers at the design question reach the design. "Make it 5 mm taller" changes the design, "cancel" cancels, and "slice it" or "print it" hands the design to intake, which still asks before anything prints. (#1208)
- A reply keeps the answer to your question, a model turn that returns no code is retried, and failure wording is honest about what happened. (#1210)
- The bot acknowledges a model turn right away and keeps typing until the part posts. (#1211)
- Geometry changes asked for from the edit chat ("cut a hole through the middle") now work like the design chat: type the next change or "cancel" at the design question, see what still needs attention (such as a part that does not fit the printer), and get told to wait if a turn is already running. With no design model configured, a design you already started still takes "undo" and plain size changes. A dropped model that is not a printable solid is shown with the problem named and is not sent to slicing from the design chat. (#1216)

### 3MF and PrusaSlicer fixes
- PrusaSlicer jobs no longer get an extra "wall" color question from the print-wide `perimeter_extruder = 1` that every PrusaSlicer config writes. The questions are the same as in 2.2.7, and saved decisions still validate. (#1202)
- 3MF conversion records real versions, the governing rules and the authoritative artifact, and per-plate filament usage drives selected-plate prompts. (#1111) (#1128) (#1129)
- The package write shell and remap refusals are hardened, and the 3MF conversion evidence page is rewritten from the current code. (#1201)

### A Web UI you can leave running
- Claimed Web UI access state that is corrupt or unreadable now fails closed instead of being treated as an unclaimed install. Raw G-code scripts get the temperature ceiling and a state gate. (#1168) (#1105)
- A stored service key is bound to the host it was set up for. Changing a service address (chat, design, agent or vision endpoint, ntfy, Matrix homeserver) can no longer send your key to a new host. (#1199)
- Web chat is private to each browser session: two people using the Web UI no longer see or answer each other's chat or questions. Uploads stream to disk instead of loading the whole file into memory, at most two are received at once, the inbox has a 2 GiB quota with a plain "inbox is full" message, and inbox files can be deleted on the upload page. Upload links expire after 7 days and are revoked whenever the Web UI login is set or changed. Passwords are hashed at 600,000 PBKDF2 iterations. Behind a reverse proxy, `webui.trusted_proxies` makes failed-login lockouts apply per client instead of to everyone at once. (#1219)
- Web chat runs the full bot in the browser. There is an upload page, a Sources page for model sources, and Telegram and Matrix settings stay hidden until configured. Settings show one section at a time, regrouped by what they serve. (#1106) (#1102) (#1100) (#1104) (#1101) (#1097) (#1099) (#1107)
- Setup is shared across surfaces: one setup-completion record, guided setup steps for platform, camera and Web UI login, catalog fields for the access settings, and a five-surface setup parity suite. (#1112) (#1117) (#1114) (#1121) (#1103)

### Unraid and Docker
- A new Compose install no longer creates a `data/hermes` folder and no longer needs you to create and chown the data folders by hand: a short init step does it. An existing install that still has `data/hermes` gets it copied into `data/state` once, never overwriting a newer file, resumable after a crash, and recorded so it never runs twice. (#1215)
- The Unraid template that binds only `/data` now keeps your state, records, cache, archive and Spoolman database in the Appdata folder. The image no longer declares `VOLUME` for six paths under `/data`, which had made Docker create anonymous volumes instead. (#1135)
- A parity test keeps the in-repo template and the Community Applications template from drifting, the recovery steps now warn against `docker volume prune`, and the settings guide points Unraid users at `.config` in the Appdata folder. (#1178) (#1108) (#1233)
- Stock Compose publishes the Web UI port, and a clean-directory first boot is tested. A new CI check follows the README's own Get started blocks on a fresh runner until the bot answers `/print-help`. (#1118) (#1167) (#1224)
- Base images are pinned by digest, GPU flavors build in CI, and the installed wheel is tested. (#1205) (#1076)

### Printer control truth
- `/print-stop` reads the heaters back after the stop and tells you if one is still on, instead of only saying "Stopped". (#1221)
- Temperatures are held to one per-printer ceiling, taken from the printer profile, everywhere Print Handy sends one: print start checks, the panic purge, OctoPrint preheat and `/print-macros run` temperature arguments. Above it, nothing is sent. The AD5X profile carries its 300 C nozzle maximum. (#1221)
- Stock-firmware AD5X prints tell the printer which IFS bay to feed from (built from the published API, not yet confirmed on a real printer). Clock sync only touches ZMod printers. A lost start on OctoPrint or PrusaLink now settles from the printer's own status, as Moonraker already did. (#1221)
- Printer start readback, stop routing, alert printer selection and the order of side effects on a mapped start are corrected, so a control command reports what the printer actually did. (#1163)
- G-code bounds recognize more spellings, refuse coordinate changes they cannot model, and keep axis and heater limits enforced. Yolo launches are checked. (#1164)

### Chat, camera and timelapse
- Chat delivery fixes: starter pings notify again, opening web chat no longer disturbs live intakes on other platforms, outgoing text can no longer make the bot attach a file it should not, and a burst of messages can no longer freeze the bot. (#1172) (#1098)
- Overlapping asks in one chat session are serialized so no turn is lost, and each session keeps bounded context. (#1084)
- Camera watch checks Moonraker prints without layer metadata on a timer, requires distinct and spaced camera views before the optional auto-pause (still off by default), and recovers from camera and detector failures. An empty bed during a Bambu print is now an alert, and a camera timeout tells you the print is unmonitored. (#1169) (#1187) (#1085) (#1086) (#1088) (#1092)
- Timelapse interval captures are scheduled against absolute deadlines, so a slow scheduler makes a frame late instead of lost. The capture settings are in the Web UI, and the completion roast posts under the timelapse. (#1179) (#1094) (#1089)
- Wrapped: percent signs in a title no longer break the video, the compile runs in the background with progress, pages are cached, sharing is link-only by default, and long timelapses are thinned instead of cut off. The Data tab is separate from Wrapped. (#1162) (#1107) (#1181)

### Licensing
- Print Handy is now AGPL-3.0-only. 2.2.7 and earlier stay MIT, and NOTICE keeps those terms. The third-party license table is complete, every Web UI page footer links the source for the running version, and each release gets an SBOM. (#1123) (#1171)

## Fixes

- The Web UI Sources page opens again. It had been failing on every install because of the MakerOnline row, which now reads "No key needed". (#1225)
- Turning Wrapped on or off on the settings page no longer freezes the Data tab at its old state. Until you set the Data tab switch yourself, it follows Wrapped, as the docs always said. (#1239)
- When a printer screen error alert failed to post, the person who started the print was never pinged for that error again. Now the next alert for that error still pings them. (#1226)
- Support analysis stops flagging bottom chamfers and stops recommending supports that cannot fit. (#1093)
- A healthy listener is no longer killed when the health probe itself crashes. (#1092)
- OrcaSlicer plugin keys are stripped from imports and protected from edits. The bundled OrcaSlicer moves to the v2.5.0 final. (#1090) (#1091)
- Roast fact banks outrank the title reaction. (#1096)
- A pre-existing closed-table key no longer blocks every Web UI save, and a save with no catalog fields still reports such a key instead of staying quiet until the next restart refuses the file. (#1103) (#1227)
- Reprints enforce the queued artifact hashes and stable duplicate-start identities. (#1078)
- The static demo wording matches the production bot. (#1177)

## Upgrade and compatibility

- **The Docker Web UI serves openly on first boot until you set a login (#1116).** There is no login by default, and the Web UI is on by default. If anyone you do not trust can reach the port, set `WEBUI_ACCESS_TOKEN` in `.env` or set a password in the setup walkthrough before you expose it. Do not forward the port from your router without a login.
- **Compose: take the new `docker-compose.yml` (#1215).** It adds two short one-shot services before the main one: `print-handy-init` creates and owns the data folders, and `print-handy-migrate` copies a leftover `data/hermes` into `data/state` once. Nothing in `data/state` is overwritten. Delete `data/hermes` once you have checked `data/state`. An old compose file keeps working, since the image still reads `/data/hermes` when it is mounted. Rolling back to 2.2.4 or earlier reads `data/hermes` as it was at upgrade time, so anything written after the upgrade is not there (the Docker guide, "State volume rename", has the manual way to carry it forward).
- **Web UI login and uploads (#1219).** Existing passwords keep working and are re-hashed at the new cost the next time someone signs in on the login page, without signing anyone out. A client that only ever sends the password as the `X-WebUI-Token` header keeps the old hash until someone signs in on the page. Upload links made before this release stop working: create new ones. Web chat history is per browser session and lives in memory, so signing in again starts a fresh conversation. If you run behind a reverse proxy, set `webui.trusted_proxies` (`PRINT_HANDY__WEBUI__TRUSTED_PROXIES`) to its address; empty, the default, trusts no proxy and changes nothing.
- **Data tab follows Wrapped while it is unset (#1239).** A save that changes only the Wrapped switch no longer pins the Data tab. If you turn Wrapped on and want the Data tab off, turn the Data tab switch off yourself.
- **Command-line `print-now` needs `--bed-clear-confirmed` (#1221).** That includes `slice` and `fetch` with `--print-now`. Chat starts are unchanged. A script that called `print-now` without the flag is now refused until you add it.
- **One heater ceiling per printer (#1221).** A temperature above the printer profile's maximum is refused, not clamped. The panic purge maximum is now the profile ceiling (300 C on the AD5X) instead of a fixed 260 C, and the Web UI ceiling on a hardened-steel AD5X drops from 450 C to 300 C. `bin/ph-force-restart` now takes a printer id.
- **Filament problems warn instead of block (#1190, #1213, #1238).** A job that 2.2.7 refused for filament reasons now asks once with the warnings named and prints if you accept. A plate with a modifier in another filament, or PrusaSlicer height changes, may now show a warning it did not show before. No existing 3MF in the test corpus gains a color question.
- **`[bot] mode = "hermes"` is removed (#1109).** A config that still sets it raises an error at load that names the removal. Delete the line and run the standalone bot or the Docker image. The Hermes install script and plugin payload are gone, and so are the `paths.hermes_bin`, `paths.hermes_agent`, `paths.hermes_restart_helper` and `discord.clarify_ui` settings. The generic `[llm]` and `[agent]` endpoints stay.
- **Default paths moved (#1159).** New native installs default to the neutral data and cache folders instead of `~/.hermes`. If the old default folder already exists and you did not set the path yourself, it keeps being used, so an existing install does not move. An explicit path always wins. The `/data/hermes` fallback in Docker still applies.
- **`wrapped_ai_video` is now `wrapped_video` (#1185).** The old name still works as an alias.
- **Moving a service URL to a new host now asks (#1199).** The first save that would send a stored key to a different host is refused until you tick "Send my service key to the new host" on the review page. A path or port change on the same host needs nothing.
- **Reprinting a whole project is a new run (#1218).** Its plates no longer count toward the original project's quantities.
- **Unraid on older tags keeps its child mappings (#1108).** A container created from an older multi-path template keeps its saved paths and variables until you edit them. The single Appdata template is for new installs. If you installed the single-path template on a 2.2.7 or older image, move your data out of the anonymous volumes before you recreate the container, using the steps in the Docker guide (#1135) (#1178). `webui.trusted_proxies` is not a template field; add it as a variable by hand if you need it.
- **License change (#1123).** AGPL-3.0-only from this release.
- The in-repo Unraid template still pins the 2.2.7 image digest. It moves to the 2.2.9 digest after the image publishes.

## Technical details and contributors

- The tech-debt sweep after the hosted-mode removal deleted dead code, made the TOML and YAML defaults match again and retired `discord.post_print_quiet_s` and `discord.daily_reset_hour`. (#1159)
- Parity probes now assert, and the `wrapped_ai_video` rename carries an alias. (#1185)
- CI and release process: publish guard, `:latest` repair and timing gate fixes (#1139), `:latest` can only be retagged to a published release and the post-release issue carries the digest check (#1234), upstream commit pins compared against release tag commits (#1134), any GitHub Action not pinned to a commit SHA fails `offline-tests` (#1235), no-reply commit addresses pass the public-text policy (#1231), a docs link and anchor check in CI and release procedure docs moved out of the user tree (#1207) (#1196), and the release-notes guide renamed with em dashes removed from the Docker and Web UI guides (#1212).
- Docs: the README quickstart on the first screen, troubleshooting led by the doctor and the three most common fixes, and the Bambu LAN and slicer-version statements corrected (#1224); the CONTRIBUTING required-checks list matches branch protection (#1236); the internal history move (#1193); a voice and accuracy pass with a first-run quickstart and privacy scrub (#1138); README screenshots moved up (#1126); PR and issue numbers removed from guides (#1077); and the 2.2.7 image digest pinned (#1081).
- Tests: Wrapped route authentication in the Web UI regression tests corrected (#1181), main's offline test red from #1165, #1168 and #1169 fixed (#1186), cumulative drift printed on every timing raise (#1170), the 19 corpus `model.3mf` files restored to their generated bytes (#1237), the slicing-speed baselines re-recorded from the 2.2.9 release run (#1244), the Compose port test renamed for what it pins (#1228), and the phase-two evidence harness and browser CI gate (#1079) (#1080).
- Housekeeping: the stale restart-helper comment dropped from `home.toml` (#1125) and review requested on every pull request (#1124).

## Verification and known limitations

- Every change above passed the offline suites and CI on its exact head before merging. The release candidate image is built from the tag and checked before publishing.
- Not live-verified for this release, by the owner's call: the AD5X release checks (a single-color and a multi-color IFS print, reprint start verdicts, preheat refusal, alert buttons and `/print-stop`, the filament warning and fallback rounds, a lay-flat reorient, the open first boot from another device, and a start with a real config) were deferred and stay tracked on #1082. So were the stock-firmware AD5X bay selection, the README quickstart on a real clean host, GPU encode and accelerated inference, and the Discord Activity sitting.
- No real-printer run of the filament warn-and-accept flow, no real-camera pass for the Bambu empty-bed alert or the Moonraker timer checks, and no live chat-platform run of the delivery fixes or the per-session web chat.
- The fit probe and lay-flat are covered by tests on exact shapes, not by every printer's slicer profile.
- Filament warnings are only warnings. An accepted warning can still produce a bad print.
