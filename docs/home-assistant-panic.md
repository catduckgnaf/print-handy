# Home Assistant panic button and printer controls

Put a stop button on a Home Assistant dashboard. The supported route is Print Handy itself: the Home Assistant integration or the Web UI control API. A direct-to-printer script is at the bottom, for one printer family, for when neither is available.

## 1. Supported route: the integration

The [`ha-print-handy`](https://github.com/catduckgnaf/ha-print-handy) integration (install it from HACS as a custom repository) gives every printer on your Print Handy roster **Pause**, **Resume**, **Cancel**, **Emergency stop**, **Firmware restart** and **Server restart** buttons, plus status sensors. The buttons call Print Handy's Web UI control API, so the same guards apply: the controls are off until you turn them on, emergency stop sends from any printer state (printing included), and a printer family with no verified stop or restart capability refuses and sends nothing.

To turn the controls on, set `webui.controls_enabled` to `true` and set a Web UI access token, because control requests are accepted only with the `X-WebUI-Token` header. A browser login cookie does not authorize them. The integration setup asks for that token. The route details are in [Web UI controls](WEBUI.md#power-send-g-code-and-exclude-object-ha-print-handy-1).

## 2. Supported route: the Web UI control API

Anything that can send an HTTP request can use the same routes the integration uses:

```bash
curl -X POST -H "X-WebUI-Token: $WEBUI_ACCESS_TOKEN" -H "Content-Type: application/json" \
     -d '{"confirm": true}' http://print-handy.example:8088/api/printers/<printer-id>/emergency_stop
```

`emergency_stop`, `firmware_restart` and `server_restart` each need `{"confirm": true}`. They call Moonraker's emergency-stop, firmware-restart and server-restart endpoints, so they apply to Moonraker (Klipper) printers.

## 3. Last resort: direct to Moonraker

Use this only when Print Handy is not running and you still need a button. It is written for a Flashforge AD5X running ZMod Klipper, and it talks to the printer directly, so none of Print Handy's checks apply. Replace the address with your printer's.

> Home Assistant's network path to the printer's Moonraker endpoint (`192.0.2.50:7125` below) must be confirmed before you rely on these commands.

### Emergency stop, then restart

An emergency stop (M112) followed by a Klipper firmware restart. In `configuration.yaml` or `rest_commands.yaml`:

```yaml
rest_command:
  printer_emergency_stop:
    url: "http://192.0.2.50:7125/printer/emergency_stop"
    method: POST
    timeout: 10

  printer_firmware_restart:
    url: "http://192.0.2.50:7125/printer/firmware_restart"
    method: POST
    timeout: 15
```

A script that runs them in order:

```yaml
script:
  printer_force_restart:
    alias: "Printer Force Restart"
    icon: "mdi:restart-alert"
    sequence:
      - action: rest_command.printer_emergency_stop
      - delay:
          seconds: 2
      - action: rest_command.printer_firmware_restart
```

On a machine with a Print Handy checkout, `bin/ph-force-restart` runs the same ladder through Print Handy (emergency stop, firmware restart, then ZMod's `REBOOT` macro if Klipper does not come back). It acts on the default printer; pass a roster printer id to restart another one, for example `bin/ph-force-restart workshop-ad5x`. An unknown id is refused and nothing is sent.

### Guarded purge

Purging extrudes filament already loaded into the toolhead. On a machine with a Print Handy checkout, use the wrapper, which holds Print Handy's control lock, refuses unless the printer is idle and Klipper is ready, checks the head filament sensor, clamps the extrusion (at most 150 mm, at least 180 degrees C), refuses a temperature above the printer's nozzle ceiling (the fitted nozzle type's safe maximum, lowered to the printer model's own maximum where the profile has one), feeds in 10 mm steps, and always sets the heater target back to 0 and reads it back:

```yaml
shell_command:
  printer_purge: "<path-to-print-handy>/bin/ph-purge --mm 60 --temp 210"
```

If Home Assistant cannot run the wrapper, this sends the purge as one G-code script. It skips every check above, and it does not reset itself on failure: the final `M104 S0` runs only if every earlier line succeeded, so an interrupted script leaves the heater at its target. Check the printer afterward and set the heater to 0 yourself if it did not finish.

```yaml
rest_command:
  printer_purge_unlocked:
    url: "http://192.0.2.50:7125/printer/gcode/script"
    method: POST
    content_type: "application/json"
    payload: >-
      {"script": "M104 S210\nTEMPERATURE_WAIT SENSOR=extruder MINIMUM=205\nM83\nG1 E10 F150\nG1 E10 F150\nG1 E10 F150\nG1 E10 F150\nG1 E10 F150\nG1 E10 F150\nM104 S0"}
```

## 4. Printer status

To read state, Klippy status, temperatures and the head sensor in Home Assistant without the integration, query Moonraker objects with a REST sensor:

```yaml
sensor:
  - platform: rest
    name: "Printer Objects"
    resource: "http://192.0.2.50:7125/printer/objects/query?print_stats&webhooks&extruder&heater_bed&filament_switch_sensor%20head_switch_sensor"
    method: GET
    scan_interval: 10
    value_template: "{{ value_json.result.status.webhooks.state | default('unknown') }}"
    json_attributes_path: "$.result.status"
    json_attributes:
      - print_stats
      - webhooks
      - extruder
      - heater_bed
      - "filament_switch_sensor head_switch_sensor"
```
