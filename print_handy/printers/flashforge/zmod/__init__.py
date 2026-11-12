"""ZMod / AD5X native code: what core reaches through PrinterAdapter.native on a ZMod printer.

multicolor_contract  tool -> slot mapping, material validation, color/MD5 finalization
native_startup       the native start-heater preamble rules
native_idle          corroborated idle gate for mapped launches
stock                stock firmware /detail: IFS occupancy and the native start gate
screen               CHANGE_ZCOLOR screen sync
Imported only when an adapter's `native` is used, so stock Klipper printers never load it.
"""
from . import multicolor_contract, native_idle, native_startup, screen, stock


def _forward(module, name, alias=None):
    """Call-time lookup, so patching a function in its own module is seen by everything that reaches it here."""
    def call(*args, **kwargs):
        return getattr(module, name)(*args, **kwargs)
    call.__name__ = alias or name
    call.__doc__ = getattr(module, name).__doc__
    return call


finalize = _forward(multicolor_contract, "finalize")
embedded_mapping = _forward(multicolor_contract, "embedded_mapping")
normalize_mapping = _forward(multicolor_contract, "normalize_mapping")
describe_mapping = _forward(multicolor_contract, "describe_mapping")
validate_materials = _forward(multicolor_contract, "validate_materials")
used_tools = _forward(multicolor_contract, "used_tools")
require_idle_settled = _forward(native_idle, "require_idle_settled")
require_idle_screenless = _forward(native_idle, "require_idle_screenless")
read_zmod_color_slots = _forward(stock, "read_zmod_color_slots")
native_startup_valid = _forward(native_startup, "valid", "native_startup_valid")
read_stock_detail = _forward(stock, "read_stock_detail")
read_stock_filament_slots = _forward(stock, "read_stock_filament_slots")
read_stock_filament_slots_from_detail = _forward(stock, "read_stock_filament_slots_from_detail")
validate_native_start = _forward(stock, "validate_native_start")
apply_stock_presence = _forward(stock, "apply_stock_presence")
screen_label_matches = _forward(screen, "label_matches", "screen_label_matches")

__all__ = ["apply_stock_presence", "describe_mapping", "embedded_mapping", "finalize", "multicolor_contract",
           "native_idle", "native_startup", "native_startup_valid", "normalize_mapping", "read_stock_detail",
           "read_stock_filament_slots", "read_stock_filament_slots_from_detail", "read_zmod_color_slots",
           "require_idle_screenless", "require_idle_settled", "screen", "screen_label_matches",
           "stock", "used_tools", "validate_materials", "validate_native_start"]
