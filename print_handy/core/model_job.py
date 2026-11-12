#!/usr/bin/env python3
"""Fetch, inspect, and (only with --approved) slice Printables, MakerWorld or Thingiverse jobs,
plus direct file links to a .stl/.3mf on any public host (#607).

Normal path, one command:
  python3 -m print_handy.core.model_job fetch URL
It verifies current hardware against Klipper + Moonraker + Spoolman, inventories every
listed source profile/file, downloads only the selected source, audits the 3MF/STL, and
creates JOB.json. It DOES NOT slice, upload, or start a print.

After the operator selects a numbered source and explicitly approves slicing:
  python3 -m print_handy.core.model_job fetch URL --select N --approved
The baked site G-code is never used on this printer. Geometry is resliced against
the live-verified nozzle, loaded Spoolman filament, and configured motion limits. Upload and
print stay separate unless --print-now is given, which means the operator clicked
"bed clear" for exactly this job. `python3 -m print_handy.core.model_job print-now FILE|JOB.json` starts an
already sliced job the same way.
"""
import argparse, base64, contextvars, hashlib, html, http.client, ipaddress, json, logging, math, os, re, shutil, socket, subprocess, sys, time, urllib.parse, urllib.request, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, NamedTuple

from .. import firmware_update, printers, settings
from ..printers.base import MultiColorUnsupported
from . import _external, filament_color, gcode_3mf, launch_state, printer_state, purge, spoolman_client, start_recorder
from . import start_truth
from .filament_warnings import FilamentWarningPending
from .site_accounts import SiteLoginNeeded
from .threemf_canonical import intake_working_copy

logger = logging.getLogger(__name__)

from ..launch.print_now import (  # moved in 2.3.0 (#1323); old path kept for one release
    BAMBU_START_CONFIRM_S, BAMBU_START_POLL_S, INTAKE_RECEIPTS, MAP_PREFIX, START_CLAIMS,
    START_CLAIM_TTL_S, START_CONFIRM_DEFAULT_S, START_CONFIRM_POLL_S, START_OUTCOME_MARK,
    START_READBACK_QUERY, StartPoll, ToolRemapRefused, Z_OFFSET_NOTICE, _FILAMENT_COLOUR_RE,
    _OUTCOME_KEYS, _RECEIPT_STARTABLE_STATUSES, _announce_polled_outcome, _announce_print_name,
    _assign_print_number, _bambu_start_confirm_window, _bambu_total_estimated_time, _bang_lines,
    _baseline_heaters_off, _baseline_heats, _check_mapped_slot_color, _clear_active_spool,
    _confirm_bambu_start, _confirm_clock, _confirm_generic_start, _confirm_mapped_start,
    _confirm_sleep, _emit_mapped_outcome, _emit_start_outcome, _entries_after_watermark,
    _filament_total_g, _find_intake_receipt, _gcode_filament_colors, _gcode_store_entries,
    _gcode_store_watermark, _maintenance_detail_line, _moonraker_started_files,
    _note_delivery_format, _owning_job, _per_tool_filament_settings, _poll_start_confirmation,
    _preparation_evidence, _print_bambu, _print_elegoo_sdcp, _print_flashforge_stock,
    _print_now_control_lock, _print_now_locked, _print_octoprint, _print_prusa, _publish_job_card,
    _read_claims, _read_start_readback, _readback_status, _reconcile_lost_start,
    _record_intake_receipt_started, _rejected_start_message, _rerecord_selected_slot,
    _settle_intake_start_claim, _settle_lost_start, _settle_polled_start, _start_baseline,
    _start_confirm_window_s, _start_confirmed, _start_family_monitor, _start_pending,
    _start_receipt_error, _start_rejection, _start_visibly_running, _store_entry_mark,
    _submission_failed, _sync_active_spool, _uncertain_after_submission, _wait_printing,
    _write_checkpoint, _write_claims, _zmod_upload_artifact, _zmod_version,
    accepted_color_mismatches, accepted_label_mismatches, claim_start_announcement,
    color_mismatch_key, job_card, job_details, label_mismatch_key, live_snapshot, mesh_line,
    print_mapped_native, print_now, record_start, release_start_announcement, remap_slot,
    z_offset_notice,
)

from ..jobs.common import (  # moved in 2.3.0 (#1323); old path kept for one release
    ARCHIVE, BED_MM, FILAMENT_SLOTS, MAX_HEIGHT_MM, MOONRAKERCTL, PROFILE, STATE, TIMEZONE, UA,
    _moonrakerctl_env, _mr, request_json,
)


from ..jobs.sources import (  # moved in 2.3.0 (#1323); old path kept for one release
    CJK, CREALITYCLOUD, CREALITYCLOUD_ANSWER_GAP, CULTS3D, CULTS_DOWNLOAD_GAP, CULTS_REFUSAL,
    DIRECT_FILE_EXTS, DIRECT_MAX_REDIRECTS, DirectLinkError, DownloadUnsupported, MAKERONLINE,
    MAKERONLINE_DOWNLOAD_GAP, MAKERONLINE_LICENSES, MYMINIFACTORY, MYMINIFACTORY_CHALLENGE_GAP,
    MYMINIFACTORY_DOWNLOAD_GAP, NEXPRINT, NEXPRINT_ANSWER_GAP, NEXPRINT_LICENSES, PRINTABLES,
    SITE_DOWNLOAD_FUNCS, SITE_HOSTS, SITE_INVENTORY_FUNCS, SUPPORTED_LINKS_MESSAGE, THANGS,
    THANGS_ANSWER_GAP, THINGIVERSE, _CC_LICENSES, _DropAuthOnHostChange, _NoRedirectHandler,
    _PinnedConnection, _PinnedHTTPConnection, _PinnedHTTPHandler, _PinnedHTTPSConnection,
    _PinnedHTTPSHandler, _REDIRECT_CODES, _answer_gap, _answer_url, _cached_maker_source,
    _cached_source, _content_disposition_name, _crealitycloud_blocked, _crealitycloud_download_url,
    _crealitycloud_profile_file, _crealitycloud_request, _crealitycloud_result,
    _crealitycloud_token_refused, _cults_blocked, _cults_graphql, _direct_name_from_path,
    _fetch_chosen, _fetch_direct_response, _is_3mf_package, _maker_refused, _mmf_blocked,
    _mmf_json, _nexprint_result, _open_once, _refuse_non_public, _resolve_host_ips, _reuse_cached,
    _sniff_direct_file, _thingiverse_blocked, _thingiverse_json, _title_slug,
    _validated_public_ips, _web_api, assert_public_url, crealitycloud_download,
    crealitycloud_headers, crealitycloud_inventory, crealitycloud_profile_id, crealitycloud_slug,
    crealitycloud_token, cults_credentials, cults_download, cults_id, cults_inventory,
    direct_download, direct_inventory, direct_link_target, download, download_for_site,
    dropbox_fetch_url, english, gql, inventory_for_url, maker_download, maker_headers, maker_id,
    maker_inventory, makeronline_download, makeronline_id, makeronline_inventory,
    myminifactory_api_key, myminifactory_download, myminifactory_id, myminifactory_inventory,
    nexprint_download, nexprint_id, nexprint_inventory, printables_download, printables_id,
    printables_inventory, thangs_download, thangs_id, thangs_inventory, thingiverse_download,
    thingiverse_headers, thingiverse_id, thingiverse_inventory,
)






































































# ---------------------------------------------------------------------------
# Login-token web-API sites (#464): Thangs and Nexprint (MakerOnline's
# inventory shares the transport but is inventory-only; see below).
#
# Like Creality Cloud above, each of these uses the JSON API its own web app
# calls. Model detail and the file list answer anonymously; the download
# needs the operator's login token (see site_accounts), which the operator
# copies from their signed-in browser into ~/.config/<site>/token. No
# password is stored, no captcha is solved, and no site check is bypassed:
# a robot check or captcha answer is a plain stop. Verified live 2026-10-29
# end to end for Thangs and Nexprint with operator tokens (inventory, the
# tokenless refusal, and a real download); answers are still parsed
# defensively, refusing with a named gap instead of guessing.
# ---------------------------------------------------------------------------











































from . import model_sources as _model_sources













































from ..jobs.store import (  # moved in 2.3.0 (#1323); old path kept for one release
    CONTEXT, RECORD_KEYS, _detail_tail, _job_adapter, _job_for_gcode, _job_printer_id,
    _job_record_for_gcode, _printer_key, _recipe_changed, _recorded_printer_id, _timing,
    decisions_digest, effective_color_decisions, gcode_estimate_s, human_dur, remote_name,
    resolve_print_target, write_job,
)


from ..gcode.artifact import (  # moved in 2.3.0 (#1323); old path kept for one release
    _text_like_read_text, file_sha256,
)


from ..gcode.verify import (  # moved in 2.3.0 (#1323); old path kept for one release
    _TOOL_CANDIDATES, _TOOL_LINE_RE, _VERIFY_LINE_PREFIXES, _already_verified_artifact,
    _full_verified_marker, _gcode_tools, _print_check_warnings, _recorded_temperatures,
    _refuse_multicolor, _run_live_printer_check, _verification_facts_digest, _verify_lines,
    _verify_offline, _verify_routed, _verify_state, _with_gcode_temperatures,
    native_heater_lines_valid, sovol_style_start_matches, verify_gcode, verify_gcode_offline,
    zmod_md5_valid,
)


from ..jobs.slice import (  # moved in 2.3.0 (#1323); old path kept for one release
    BED_LIMIT_MM, INCH_TO_MM, ImplicitPlateSplit, NoPrintableVolume, PAINTED_SCALE_STEPS,
    UNIT_QUESTION_MAX_MM, _choice_label, _edit_kw, _enforce_filament_readiness,
    _enforce_filament_warnings, _fallback_selection, _filament_ack_covers, _gcode_object_count,
    _is_generated_mm, _job_fit, _merge_edge, _painted_fit, _painted_fit_for_job, _painted_tower_mm,
    _part_slots, _per_part_view, _placed_bounds_by_object, _plates_for,
    _rearranged_group_placement, _slice_full_size_split, _slice_merged, _slice_merged_multi_tool,
    _slice_painted_plates, _slice_per_plate, _support_kw, _whole_source_usage_warnings,
    copies_supported, filament_fallback_slot, filament_fallbacks, finalize_multicolor,
    full_size_one_plate, model_info_output, normalize_units, painted_fit, painted_fit_for_job,
    painted_plate_layout, painted_plate_scale, per_part_fit_options, plate_manifest,
    prepare_first_layer, refuse_assembly_split, single_color_groups, slice_job, slice_multi_plate,
)










from ..jobs.audit import (  # moved in 2.3.0 (#1323); old path kept for one release
    audit, mesh_audit, mesh_topology,
)






from ..jobs.store import (  # moved in 2.3.0 (#1323); old path kept for one release
    _recipe_binding, intake_folder, intake_id_from_message, record_support_decision, safe_slug,
    save_filament_selection, set_color_decisions, stamp_job_printer,
)




from ..jobs.prompts import (  # moved in 2.3.0 (#1323); old path kept for one release
    IntakeSpoolWeights, _audit_dims, _check_tool_assignments, _reference_manifest,
    _selected_plate_stats, _unit_packet, _usage_for_source_extruder, filament_usage_by_selection,
    format_spool_projection, get_author_settings, intake_color_choices, intake_spool_projection,
    intake_spool_weights, intake_unit_confirmation, make_unit_decision, parts_unit_question,
    record_filament_warning_acknowledgement, retry_pending_spool_sync, set_unit_decision, show,
    source_site_estimate, unit_question,
)

from ..gcode.bounds import (  # moved in 2.3.0 (#1323); old path kept for one release
    BOUNDS_CACHE, BOUNDS_CACHE_MAX_ENTRIES, BOUNDS_PARSER_VERSION, NUMBER,
    _BOUNDS_NATIVE_API_VERSION, _BOUNDS_NEVER_SWALLOW, _BOUNDS_SELF_CHECK_OK,
    _BOUNDS_SELF_CHECK_REFUSALS, _GCODE_FORBIDDEN_NAMES, _GCODE_G10_OFFSET_RE, _GCODE_G_FORBIDDEN,
    _GCODE_G_KNOWN, _GCODE_G_WORK_OFFSETS, _GCODE_LINE_RE, _GCODE_OFFSET_NAMES, _GCODE_STRAY_CR_RE,
    _GCODE_XYZ_RE, _GCODE_XYZ_RE_I, _bounds_impl_setting, _cached_bounds,
    _gcode_motion_bounds_python, _load_native_bounds, _native_bounds_result, _prune_bounds_cache,
    _reset_native_bounds_cache, cached_motion_bounds, gcode_motion_bounds,
)


from ..jobs.filament import (  # moved in 2.3.0 (#1323); old path kept for one release
    FILAMENT_SENSOR_OBJECTS, _validate_slot_selection, apply_stock_presence, configured_slots,
    position_limit, read_filament_telemetry, read_stock_detail, read_stock_filament_slots,
    read_stock_filament_slots_from_detail, resolve_filament_selection, telemetry_state,
    validate_filament_selection,
)


def format_color_title(c):
    return ' '.join(w.upper() if w.casefold() in {'pla', 'pla+', 'petg', 'petg-cf', 'abs', 'tpu'} else w.capitalize() for w in str(c or '').split())


def _build_position_labels(selections, noun):
    counts = {}
    for s in selections:
        c_title = format_color_title(s.get('color'))
        mat = printer_state.display_material(s.get('material', ''))
        name = c_title if mat and c_title.casefold().endswith(mat.casefold()) else f'{c_title} {mat}'.strip()
        counts[name] = counts.get(name, 0) + 1
    labels = []
    for s in selections:
        c_title = format_color_title(s.get('color'))
        mat = printer_state.display_material(s.get('material', ''))
        name = c_title if mat and c_title.casefold().endswith(mat.casefold()) else f'{c_title} {mat}'.strip()
        if counts[name] > 1:
            labels.append(f'{name} ({noun} {s["slot"]})')
        else:
            labels.append(name)
    return labels


def build_slot_labels(selections):
    return _build_position_labels(selections, 'Slot')


def build_tool_labels(selections):
    """Choice labels for a multi_tool printer: positions are toolheads, never slots (#346)."""
    return _build_position_labels(selections, 'Toolhead')












from ..launch.gates import (  # moved in 2.3.0 (#1323); old path kept for one release
    BED_CLEAR_REFUSAL, CHECKPOINT_NOT_SENT, IDLE_PRINT_STATES, MESH_PROFILE,
    READINESS_MAX_AGE_SECONDS, YOLO_WAIVED_MARK, _BED_CLEAR, _HEATER_ARG_RE, _HEATER_MACRO_RE,
    _HEATER_M_RE, _HEATER_SET_RE, _HEATER_VALUE_RE, _YOLO, _claim_bed_clear, _consume_approval,
    _gate, _gcode_heater_targets, _heater_limits, _native, _printer_phase, _refuse,
    _require_bed_clear, _require_idle, _require_idle_before_mapped_launch,
    _require_launch_checkpoint_clear, _verify_filament_readiness, _verify_readiness_adapter,
    _waive, check_filament_readiness, mesh_readiness, partial_job_refusal, refused_plates_digest,
    require_current_artifact, require_mechanical_limits, screen_z_offset, start_print_settings,
    validate_native_start, validate_saved_mesh_start, yolo_active,
)





































































































































































def record_supports_question(folder, audit):
    """Record why the supports question was (or was not) shown (#1020 F3).

    ``audit`` carries the decision's reason and inputs plus the
    clearance report and the slicer finding the decision was made from,
    so a shown or hidden menu can be audited from JOB.json alone.
    """
    path = Path(folder) / 'JOB.json'
    job = json.loads(path.read_text())
    job['supports_question'] = {**audit, 'at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    write_job(folder, job)
    return job
































































































def _fetch_remote_gcode_sha256(moonraker_url, remote_name, timeout=60):
    escaped = urllib.parse.quote(remote_name)
    url = f"{moonraker_url}/server/files/gcodes/{escaped}"
    hasher = hashlib.sha256()
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total_read = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
            total_read += len(chunk)
            if total_read > 1024 * 1024 * 1024:
                raise RuntimeError(f"Remote file {remote_name} exceeds 1GB limit")
    return hasher.hexdigest()




# The Moonraker start truth (#1318): the baseline classifier and its helpers live in start_truth.
StartReadback = start_truth.StartReadback
classify_start_readback = start_truth.classify_start_readback
_same_job = start_truth._same_job
















def _fmt_slots(slots):
    if not isinstance(slots,list) or not slots: return "unknown"
    def u(v): return v if v not in (None,"") else "unknown"
    def loaded(v): return "loaded" if v is True else "empty" if v is False else "presence unknown"
    return "; ".join(f"{u(s.get('slot'))}: {u(s.get('color'))} {u(s.get('material'))} ({loaded(s.get('loaded'))})" for s in slots)


from ..jobs.cli import (  # moved in 2.3.0 (#1323); old path kept for one release
    ROUTINE_NAMES, ROUTINE_TYPICAL_MIN, _main, _print_filament_warning_packet, _print_now_cli,
    main, routine_phase, routine_receipt,
)


















def _native_file_matches(reported, remote):
    """Match only documented Klipper paths for this upload, including ZMod's 3MF unpack path."""
    return launch_state.same_remote(reported, remote)






def _reconcile_native_submission(st, remote):
    """Read status once after lost POST response; never attribute heaters alone to this job."""
    return _read_start_readback(st, remote)[1]


































# Normalized states that mean "accepted, still starting" (heating the bed, running its calibration).
START_TRANSITIONAL_STATES = start_truth.START_TRANSITIONAL_STATES






def _reports_started(live, remote):
    """Printing or a transitional state, naming this upload (launch_state.same_remote): start_truth.polled."""
    return start_truth.polled(live, remote).confirmed








































def _bambu_reports_file(live, remote):
    """True when a normalized Bambu status shows `remote` running or preparing.

    `printing` covers gcode_state RUNNING, PREPARE and SLICING: an A1 unpacking a package sits
    in PREPARE before RUNNING, and both prove the start reached it. The file name is the
    `subtask_name` the start command set, so exact; the bare project name (the printer's
    display spelling of the same package) is accepted too. The rule is start_truth.bambu's.
    """
    return start_truth.bambu(live, remote).confirmed








































# The argument names are heater_ceiling's, shared with the macro run guard (#1147).
from .heater_ceiling import MACRO_BED_ARGS as _MACRO_BED_ARGS, MACRO_NOZZLE_ARGS as _MACRO_HOTEND_ARGS  # noqa: E402








def yolo_waivers(tail):
    """The `YOLO WAIVED:` lines of one print-now output, without the mark."""
    return [line.split(YOLO_WAIVED_MARK, 1)[1].strip()
            for line in str(tail or "").splitlines() if YOLO_WAIVED_MARK in line]


# Intake receipt reconciliation (#970). The chat intake leaves a sliced job's
# receipt at SLICED_NOT_STARTED until its own button flow starts it. Starting
# the same artifact out-of-band with `model_job print-now` used to leave the
# receipt there: chat could then offer to start a job already on the printer,
# and the receipt history was wrong (intake 16fd2142…, printing as print #83,
# still "not started"). A confirmed print-now start therefore moves the
# owning receipt to STARTED itself, records that the start was manual
# (started_via: print-now) and the print id, and settles the chat one-start
# claim so a later chat start of the same target is refused as a duplicate.

























# Code moved out of this module (#1323) still sees assignments made here
# (mock.patch, monkeypatch) for one release; see print_handy/_relocated.py.
from .. import _relocated  # noqa: E402
from ..gcode import artifact as _gcode_artifact, bounds as _gcode_bounds, verify as _gcode_verify  # noqa: E402
from ..jobs import common as _jobs_common, filament as _jobs_filament, store as _jobs_store  # noqa: E402
from ..jobs import audit as _jobs_audit, cli as _jobs_cli, prompts as _jobs_prompts  # noqa: E402
from ..jobs import slice as _jobs_slice, sources as _jobs_sources  # noqa: E402
from ..launch import gates as _launch_gates, print_now as _launch_print_now  # noqa: E402
_relocated.keep_old_path(__name__, _gcode_bounds, _gcode_verify, _gcode_artifact, _jobs_common,
                         _jobs_store, _jobs_filament, _launch_gates, _launch_print_now, _jobs_slice,
                         _jobs_sources, _jobs_audit, _jobs_prompts, _jobs_cli,
                         live=("_native_bounds_module", "_native_bounds_broken"))
if __name__=="__main__": main()
