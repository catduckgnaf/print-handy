#!/bin/sh
# ZMod plugin lifecycle hook: uninstall.
# These three names are fixed by the plugin host, not ours: install.sh, update.sh and
# uninstall.sh are the lifecycle hooks it looks for by exact name, so they stay at the
# repository root with these names (#1196). They are not Print Handy install scripts;
# for those see docs/INSTALL.md and scripts/install-standalone.sh.
# ZMod runs this, then removes the plugin directory.
echo "print-handy uninstalled"
echo "Remove the [gcode_macro _PRINT_HANDY_VARS] block from mod_data/user.cfg."
