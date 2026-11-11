# Best practices on specific devices

The rest of the documentation describes what Print Handy supports. These
pages are narrower: for one printer, the settings and habits that work
well with Print Handy, the order to change things in, and what has and
has not been checked on real hardware.

Each page names the firmware and hardware it was written against. If
your printer runs something else, treat the page as a starting point,
not a contract. The contracts stay in their own pages
([ZMod printers](ZMOD.md), [AD5X implementation contract](history/AD5X-CONTRACT.md) (a developer record),
[Printer calibration](CALIBRATION.md), [Settings reference](SETTINGS.md)).

## Devices

| Printer | Page | Written against |
| --- | --- | --- |
| Flashforge Adventurer 5X (AD5X) | [AD5X best practices](DEVICE-AD5X.md) | ZMod 1.7.5-2, Klipper 13, screenless with HelixScreen |

More devices will follow. A page is added when someone runs Print Handy
on that printer long enough to say what works, with the hardware
evidence to back it.

## Writing a device page

- Lead with the tested baseline: firmware, Klipper, screen mode, add-ons.
- Link to the contract pages instead of restating them.
- Use the real setting and macro names from the code on main.
- End with a table of what was verified on hardware and what was not.
  Offline tests and upstream documentation are not hardware evidence.
- Leave out addresses, hostnames, tokens and anything else personal.
