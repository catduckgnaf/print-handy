# Print Handy 2.3.2: it prints what the designer drew

One Docker image for amd64 + arm64: ghcr.io/catduckgnaf/print-handy:2.3.2, also tagged latest.

A pinecone lantern walked into 2.3.1 and the bot panicked. It called the scales a defect, pushed supports the designer had switched off, told you to repair the geometry, refused an AI fix because it counted six print profiles as six files, asked about the same short spool twice, and then sliced the whole thing at 0.24 mm with 15% line infill instead of the designer's 0.20 mm and 5% gyroid. 2.3.2 is what we fixed after that, plus everything an independent review of the chat process and a new way of testing the corpus turned up along the way.

## What you can do

### Print it the way the designer set it up
- Models from a Bambu Studio, OrcaSlicer or PrusaSlicer project print at the layer height and first layer their designer chose, as long as they fit your fitted nozzle's range (0.08-0.28 mm on a 0.4 mm nozzle). A project set up for a different nozzle gets a note. Before, every job was forced to 0.24 mm, which OrcaSlicer ships as the AD5X *Draft* height. A value outside the range is pulled to the nearest limit and the slice record says so; the 0.24 mm default only applies when the project sets nothing (an STL, a plain 3MF). Expect longer prints on 0.20 mm projects: that is the designer's choice, and OrcaSlicer's own AD5X default. (#1378)
- Support gaps follow the layer height they were designed for, the way OrcaSlicer's AD5X profiles pair them, and the designer's support XY distance is kept. (#1378)
- A project with Arachne walls keeps its own infill, first layer and supports. It used to get a whole "print-in-place" profile swapped in (15% line infill, supports off). That profile now only steps in when OrcaSlicer rejects a slice. (#1378)
- A multi-plate project too big for your bed no longer quietly stands the designer's parts on their side over supports. A pose without supports always wins, each tipped part says why ("too tall to stand as designed"), and the question offers the same split slightly smaller with every part the way its designer laid it out: a 23-plate tray set prints at 99.8% with every tray flat instead of 100% with most of them on edge. Full size is still one tap away. (#1380, #1383)

### Print news in the Web UI chat
- The Web UI chat page now gets the same print posts as your chat app: started, paused, done, cancelled, errors, the roast and the next-plate offer. A pause, an error, an emergency stop or a camera check comes with its buttons (Resume, Cancel, It's fine and friends), and a press there works exactly like one in Discord, Telegram or Matrix: first answer wins, and the printer is re-read before anything is sent. Photos and timelapses are named, not shown. (#1384)

### Fewer questions that were never real
- Models whose designer prints them without supports no longer get a supports question or a "repair the geometry" warning for overhangs inside the same part, like stacked scales or stair steps. A gap over a different part, or in a file that says nothing about supports, is still checked. (#1378)
- A short spool you already saw on the Ready screen is not asked about again when the print starts, as long as it is the only short spool and the slice needs no more than was shown. Multi-color jobs and yolo runs still ask or waive at the start. (#1378, #1379)
- The with-supports option shows its own time and filament before you can pick it, including after Reorient or Scale, and you can press Stop while it is slicing to compare. (#1378)
- "Cancel the print" in chat goes straight to the stop confirmation, and the cancel post is the print's name plus one line. Anyone allowed can still press Stop on a slice or start in progress. (#1378, #1379)

### Spools that add up
- A single-slot print can no longer charge a spool for everything the printer extruded since it last restarted (a 0.6 g print once charged 987 g). Print Handy notes the spool's weight at launch and corrects a runaway charge at the end. The cause is in Moonraker and is reported upstream. (#1375)
- Edit a spool's remaining weight, swap the spool in a slot, or add an active spool in one step in the Web UI, and correct a weight from Discord with `/spool weight`. Archived spools are hidden by default. (#1375)

### AI is for designing models
- Chat AI and AI camera checks are off by default. `/print-design` (and asking it to "fix this file") still uses AI. Turn chat back on with `discord.chat_llm_enabled`, and camera AI with `vision.ai_enabled`. (#1377)
- Intakes no longer end with a "Fix this for my printer" button. (#1378)

## Fixes
- "Without supports" could print with supports when the designer's project had them on: the first slice ran before you chose, and was reused. It slices again now. (#1378)
- "Print anyway (accept the risk)" could start a print on an install set not to print, and recorded a bed-clear check it never asked for. It now says "(bed is clear)" and only prints where printing is on. (#1378)
- On Discord, anyone in the channel could press the buttons of someone else's question. Now only the person asked, or an approver, can. (#1378)
- The clearance check now looks at the model the way it will print: turned, laid flat or scaled, with your real first layer (PrusaSlicer projects included). A gap between two parts is never treated as squished shut. Extend and sink are no longer offered on a turned model, where they would have cut the wrong side. (#1378, #1379)
- A chat edit of the first layer height now reaches the slicer; before, only the clearance check saw it. (#1379)
- Reorient no longer overwrites a turn that was needed to fit the bed, and is not offered on split plans or with several copies. On a model that already lies flat it no longer re-slices for nothing. (#1378)
- A full-size split that stands parts up says on the Ready screen that those parts keep their supports. (#1378)
- "With supports" on a project set to manual support placement with nothing painted now places supports automatically instead of none. (#1378)
- The roast keeps a long title's name ("Pinecone Cottage Lantern", not "... - Hanging or"). (#1378)
- A cancel on one printer is no longer mistaken for the bot's cancel of another. (#1378)
- `/print-stop` on one printer could name a start that was in flight on another. Start claims are kept per printer now. (#1382)
- A print cancelled while the listener was down never got its cancel post. It is posted once when the listener comes back. (#1382)
- The runaway-charge guard from #1375 only armed on some launch paths; it now arms on every real launch, and a few multi-slot charge cases add up correctly. (#1381)

## Upgrade and compatibility
- **Prints may take longer.** Projects designed at 0.20 mm (most MakerWorld profiles) now slice at 0.20 mm instead of 0.24 mm, about 20% more layers. Support gaps follow layer height too: 0.16 mm becomes 0.18 mm at the default 0.24 mm layer, on every supported print.
- **Arachne projects keep their own settings.** They no longer get the print-in-place flex profile up front; a print-in-place model slices with its designer's settings, and the flex profile steps in only if OrcaSlicer rejects the slice.
- **AI camera checks are off** (`vision.ai_enabled`, a new setting, defaults to false): the endpoint camera watchdog, the idle plate checks (auto light-off, the next-plate offer), label photo reads and purchase intake stop calling a vision model. The local failure detector is unaffected. (#1377)
- **Chat AI defaults to off, but an existing install may still have it on.** An older `docker-compose.yml` sets `PRINT_HANDY__DISCORD__CHAT_LLM_ENABLED` to `true`, and a config made before 2.3.2 says `webui.chat_llm_enabled: true`. Remove that compose line and set both keys to `false` to turn chat AI off. (#1377)
- **Buttons belong to the person asked.** On Discord a channel member can no longer answer another member's question; approvers still can, and with no approval user configured nobody else can. Stop stays open to everyone allowed. (#1378, #1379)
- **Renamed buttons** (for anything that matches them): "Print anyway (accept the risk)" is now "Print anyway, accept the risk (bed is clear)", or "Slice anyway (accept the risk)" where printing is off; the not-recommended supports button is "Slice and print with supports, not recommended (bed is clear)"; "Fix this for my printer" is gone from intakes.
- **The Ready question can take longer on a flagged model** while both slices finish (up to about two minutes each), now with a Stop button.
- Spools charged wrong by the Moonraker bug before 2.3.2 are not corrected automatically: check single-slot spools that look empty. (#1375)
- **Web UI chat starts with the Web UI.** With the chat page on, web chat now starts when the server starts (before, only with ZMod update alerts on), and the listener appends its posts to `webui-chat-feed.jsonl` in the state folder (capped at 256 KiB plus one older copy). Posts from before web chat started are not replayed. (#1384)
- **Split questions have a new button.** "N% · split K plates as designed" sits after the full-size split; typing `split` or `100%` still means full size. (#1383)

## Technical details and contributors
- The corpus is now judged on the conversation, not just the receipt. `harness.conversation_checks` runs for every case, and `harness.explore` re-runs each case once per offered button at every question and holds every path to product rules. The first full walk covered 56 cases and 768 paths. The live lantern and 13 real intakes from a live install joined the private tier. (#1378)
- Layer heights: `nozzle.resolve_layer_heights` and `resolve_support_gaps`; the Arachne swap is behind `author_settings.FLEX_ON_ARACHNE`. (#1378)
- Spool guard: `multislot_usage.launch_baseline` and `guard_single_slot`; upstream report Arksine/moonraker#1125. (#1375)
- Slicing-speed baselines re-recorded from the release commit's CI run. The intake that asks the shrink question takes about 0.27 s longer since #1378 (bisected across main runs; the cause inside #1378 is not isolated yet). (#1388)
- A review of everything since 2.3.0 ran before the tag; its fixes are #1379, #1381 and #1382.
- The nightly build moves to 09:17 UTC. (#1386)
- The 2.3.1 image digest is pinned in docs/DOCKER.md. (#1372)

## Verification and known limitations
- Offline suites, the corpus with the new conversation checks and explorer, and CI on every merged head. The lantern, the private Duplo and cute-dino reproduce their bugs on 2.3.1 and pass on 2.3.2.
- Not seen on a printer yet: a 0.20 mm designer project printed end to end, the runaway-charge correction on a real Moonraker restart, the Stop button during the comparison slice, an as-designed split sliced by real OrcaSlicer and printed, and print posts with buttons in a running Web UI chat.
- Telegram and Matrix still show pause and camera alerts from Bambu, PrusaLink, OctoPrint, SDCP and stock Flashforge printers as plain text; only Klipper printers get buttons there.
