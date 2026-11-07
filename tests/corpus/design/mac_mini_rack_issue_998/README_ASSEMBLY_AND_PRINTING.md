# Mac mini (Late 2014) | 19 inch rack | 2U | PLA version 1.0

**Three structural 3D printed pieces.** Designed for the Flashforge AD5X 220 × 220 × 220 mm build area, using standard rack hardware and M4 metal nuts/bolts. Geometry is dimensioned and manifold checked but **not physically printed, load-tested, or certified**. Inspect, test-fit, and load-test before trusting it with your computer.

## Files

- \`01_CENTER_TRAY_214x214.stl\`: single ventilated Mac mini cradle, 214 × 214 mm bed footprint. Fits the nominal 220 mm AD5X plate with just 3 mm margin on all sides. Disable skirts/brims/prime towers for this part.
- \`02_RIGHT_2U_ARM.stl\`: right rack ear, deep box beam, cantilever gusset, under-tray lap joint. Print flat as provided.
- \`03_LEFT_2U_ARM.stl\`: matching left bracket. Print flat as provided.
- \`00_TEST_LOWER_M4_NUT.stl\` and \`00_TEST_UPPER_M4_BUTTON_HEAD.stl\`: **print these first** to test your actual nut/screw head fit. Stack the two test coupons with the counterbore on top and the hex pocket at the bottom.
- \`.step\` files: editable parametric CAD geometry for the three pieces, plus an \`ASSEMBLY_...step\` already assembled in correct rack coordinates. \`REFERENCE_MAC_MINI_ENVELOPE...step\` is only for checking clearances, **not** a part to print.
- \`generate_rack_mount.py\`: source to regenerate the CAD in CadQuery, edit dimensions and rebuild STLs/STEP.
- \`PREVIEW_assembled_rack_mount.png\`, \`DIMENSIONED_OVERVIEW.png\`: illustrations.

## Sized against checked manufacturer and rack data

- Late 2014 Mac mini: approximately **197 W × 197 D × 36 H mm**, about **1.2 kg**.
- Standard 19 inch rack: front panel width **482.6 mm**; opposite vertical rack-hole centerlines **465.1 mm** apart.
- Mount height: **2U = 88.9 mm**, printed mount stays inside **0 to 88.5 mm** of that envelope in the assembled geometry, not counting rack bolt heads.
- Four rack screw slots on each ear. Vertical centers from bottom of 2U are **6.35, 22.225, 66.675, and 82.55 mm**. Each rack slot is approximately **9.8 wide × 7.8 high mm** for tolerance.
- Vented tray center area supports the Mac without a solid full-width floor. Back ports face rear. Two Velcro straps pass through paired slotted passages under the cradle.
- Rack depth: the tray reaches about **240 mm behind the front face**, before plugged-in connectors. Provide at least **300 mm** of usable open space behind rack mounting face plus whatever rear cable bend radius is required.

## Hardware to obtain

1. **Twelve M4 × 16 mm low-profile BUTTON HEAD machine screws**, head diameter **≤ 8.5 mm**, head height **≤ 3.3 mm**. Avoid standard tall socket cap heads; their heads may protrude into the Mac's underside.
2. **Twelve regular full-size M4 hex nuts**, nominal 7 mm across flats. Nut pockets are about 7.32 mm across flats and 4.1 mm deep. These nuts sit flush in the under-tray lap plates.
3. **Eight appropriate rack screws** and washers for your rail type. For M6 square-hole cage nuts: use eight M6 cage nuts and eight appropriately long M6 screws, typically 20–25 mm due to the 14 mm printed ear thickness; actual rail and washer stack may differ. For tapped 10-32 or 12-24 racks, use matching screws instead of forcing M6.
4. **Two hook-and-loop straps approximately 20 mm wide × 600 mm long** to secure the Mac to the tray. Choose reusable straps or buckled straps that cannot pull loose; do not rely on loose friction fit alone.
5. Optional: a small amount of removable nonconductive silicone rubber padding, away from intake and exhaust paths, to protect the Mac's finish.

## Fit-test first

Print the two little fastener test coupons at your chosen PLA settings. Push an M4 hex nut into the underside of the lower coupon, stack the upper coupon on it with the 8.8 mm head recess facing up, and thread a **16 mm under-head-length** button screw through the stack. Check that (a) the head sits *below* the upper surface, (b) the fastener engages the nut fully, (c) the tip does **not** extend out the underside, and (d) the nut pocket doesn't split. If it fails, modify the CAD parameters or use suitable fasteners before printing the real pieces.

## Printing on the AD5X

Recommended conservative PLA starting point, using a 0.4 mm nozzle: **0.20 mm layers; 6 perimeters; 7 top/bottom solid layers; 50% gyroid infill; single-color mode**. Select print temperature for your actual PLA/PLA+ spool, and moderate outer-wall speed to maintain layer bonding. Print each of the three structural parts in separate jobs. Keep the flat back of the tray and the flat bottoms of the rack arms on the bed. Avoid enabling auto rotation that tips a bracket onto its side.

Each STL is individually positioned with its lowest surface at Z=0. The rack arms' footprints are about **172.3 × 188 mm**. The center cradle is **214 × 214 mm**, leaving only **3 mm** nominal per side; DO NOT use a brim, skirt, or purge tower. Disable any automatic prime-tower setting. Confirm OrcaSlicer accepts the whole cradle within the configured build shape and that the generated toolpath stays on the plate. You may need to adjust first-layer adhesion, the printer's available toolhead exclusion zones, or use another bigger printer if the effective bed is less than 214 mm.

Test slicing with **supports off** initially. The small captive-nut hex recesses and rack slots have short printable bridges, but inspect layer preview for unsupported areas. Enable localized supports only where needed (prefer build plate only); avoid supports inside all six nut pockets unless absolutely necessary.

Use PLA+ if available, but do not assume PLA+ fixes thermal creep. **PLA can slowly deform under stress or soften if the rack heats up.** Do not expose the bracket to an enclosed rack with elevated temperatures, direct solar heating, or a hotter unit immediately below. For unattended critical applications or warm rack environments, use PETG/ASA or, preferably, a rated metal shelf instead.

## Assembly

1. Print the coupons; verify your exact nut and button screw fit. Print the tray and mirrored rack arms. Deburr nut pockets, strap openings, and bolt holes as necessary. Ensure no cracks or layer separations.
2. With the printed arms upside down, push **six M4 hex nuts into the hex pockets of each arm**, all flush. Use tape to hold them temporarily if needed, but **do not** depend on glue as a structural joint.
3. Lay the center cradle onto the two under-tray support flanges, aligning all **twelve** through holes. Screw the twelve M4 button heads down into the captive nuts from inside the cradle's top face. They should sit recessed, so the Mac cannot rest on exposed steel screw heads. Hand tighten evenly; do not crush the PLA or strip a pocket.
4. Mount the empty 2U assembly in the rack with up to **four rack screws and washers per ear**. Use hardware appropriate for your rack. Because these are PLA ears, use appropriately wide washers to distribute bearing loads and do not overtighten. Support the cradle by hand while attaching it.
5. Test the empty bracket for flex, cracks, interference with adjacent gear, and cable clearance. Then place the Mac in the tray **right side up**, back ports facing backward. The rim and corner details have clearance for its 197 mm body. Inspect around the rear exhaust and underside intake gap; keep them unobstructed.
6. Pass the two 20 mm straps up through the paired slots at Y≈116 mm and Y≈180 mm, wrap them over the Mac, and fasten securely. They should retain the Mac without compressing or covering any ports. Add removable pads only where necessary to prevent scratching or rattling.
7. Check deflection after an hour, after a day, and periodically thereafter, especially if running the computer continuously. Never stack other equipment on this printed mount. A secondary safety tether is sensible for an occupied rack.

## Design choices / limitations

- **Six M4 bolted joints per side**, including two near the rear, resist twisting better than simple front-only dovetails.
- **Tall side gussets**, substantial front box beams, and strong side rails supply the structural load path. No press-fit or brittle snap-fit joints carry primary load.
- **Captured metal nuts** and mechanical rack screws avoid putting rack loads through printed threads.
- A **vented underside, open back, and low front opening** preserve access and cooling. The 2014 Mac mini intakes near the bottom cover and vents at the rear; leave both routes free.
- All major pieces are suitable in XYZ dimensions for a 220 mm bed. The cradle is near the nominal bed limit and assumes no slicer no-print margin >3 mm.
- This is a prototype. **No certified load capacity or long-term creep guarantee** is claimed. Test fit your specific Mac and rack before relying on it, and use a metal support for critical installations.

## Technical sources

- [Apple Mac mini Late 2014 technical specifications](https://support.apple.com/en-ca/111931)
- [Flashforge AD5X official specifications](https://www.flashforge.com/products/flashforge-ad5x-3d-printer)
- [Standard EIA rack horizontal and vertical mounting dimensions](https://www.ddbunlimited.com/accessories/racking-spec-layout/)
- [Apple Mac mini guide showing underside inlet and rear exhaust](https://www.gainsaver.com/content/manuals/Mac_mini_Mid2010_User_Guide.pdf) (this illustrated manual is for a closely related earlier Mac mini, used as airflow guidance, not proof of exact 2014 internal geometry)

**License:** public-domain CC0-1.0 for these generated designs and source. Produced as an untested personal-use prototype. Modify, print and test at your own risk.