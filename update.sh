#!/bin/sh
# ZMod plugin lifecycle hook: update.
# These three names are fixed by the plugin host, not ours: install.sh, update.sh and
# uninstall.sh are the lifecycle hooks it looks for by exact name, so they stay at the
# repository root with these names (#1196). They are not Print Handy install scripts;
# for those see docs/INSTALL.md and scripts/install-standalone.sh.
# Moonraker runs this after the update manager replaces the plugin files.
echo "print-handy updated"
echo "Spool values in mod_data/user.cfg were not changed."
echo "The macro file is print-handy.cfg (renamed from print_handy.cfg). If you"
echo "included print_handy.cfg by hand, point that include at print-handy.cfg."
