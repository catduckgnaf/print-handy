#!/bin/sh
# ZMod plugin lifecycle hook: install.
# These three names are fixed by the plugin host, not ours: install.sh, update.sh and
# uninstall.sh are the lifecycle hooks it looks for by exact name, so they stay at the
# repository root with these names (#1196). They are not Print Handy install scripts;
# for those see docs/INSTALL.md and scripts/install-standalone.sh.
# ZMod runs this from the plugin directory after ENABLE_PLUGIN name=print-handy.
# ZMod itself adds [include plugins/print-handy/print-handy.cfg] to
# mod_data/plugins.cfg; the cfg file must keep the plugin's name or ZMod
# refuses to enable the plugin. This script does not write printer config.
echo "print-handy installed"
echo "Copy the four slot colors and materials into mod_data/user.cfg, then FIRMWARE_RESTART."
echo "Query with PRINT_HANDY. It reports matches. It does not switch filament."
