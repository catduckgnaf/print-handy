"""Deterministic Discord choices, using the installed adapter and clarify callbacks.

Called on the gateway event loop by the profile plugin. Never slices or prints.
"""
import asyncio
import json
from pathlib import Path
import uuid

from .. import settings


def _visible_choices(question, choices):
    """Return the Discord prompt and at most four suggested button choices."""
    visible = list(choices[:4])
    prompt = question
    if len(choices) > 4:
        prompt += f" (Showing 4 of {len(choices)} options; additional options exist. Reply to request the remainder.)"
    return prompt, visible


async def collect(job_path, state, adapter, chat_id, session_key, metadata=None, timeout=None):
    from .prompts import gateway as _prompts
    cm = _prompts()
    from .. import printers
    from .model_job import (intake_color_choices, set_color_decisions, resolve_filament_selection,
                            write_job, decisions_digest, build_slot_labels, build_tool_labels,
                            configured_slots, position_limit, _job_adapter,
                            FilamentWarningPending, record_filament_warning_acknowledgement)
    from .source_colors import (inventory, prompts, canonical_prompts, grouped_prompts, validate_decisions,
                                distinct_source_colours, prompt_source_colour)
    path = Path(job_path)
    timeout = settings.current()["discord"]["question_timeout_s"] if timeout is None else timeout
    job = json.loads(path.read_text())
    manifest = inventory(job['downloaded_file'])
    source_hash = manifest['source_sha256']
    schema = job.get('color_schema')
    if schema not in (None, 'part_slots_v1'):
        raise ValueError('unknown color decision schema')
    # A job with a plate selection is asked only about the selected plates'
    # colors; unselected plates never prompt (#1110 Phase B).
    selected_plates = job.get('selected_plates') or None
    questions = (canonical_prompts(manifest, selected_plate_ids=selected_plates)
                 if schema == 'part_slots_v1' else prompts(manifest, selected_plate_ids=selected_plates))
    if job.get('color_manifest', {}).get('source_sha256') != source_hash:
        raise ValueError('source changed before Discord presentation')
    job_adapter = _job_adapter(job)
    multi_tool = job_adapter.multi_tool
    selections = [resolve_filament_selection(state, dict(label, slot=int(slot)))
                  for slot, label in sorted(configured_slots(state).items())]
    # A multi_tool printer's positions are toolheads, never slots (#346).
    labels = build_tool_labels(selections) if multi_tool else build_slot_labels(selections)
    if not labels or len(labels) > position_limit(job_adapter) or len(set(labels)) != len(labels):
        raise ValueError('Discord requires one to four distinct configured toolheads' if multi_tool
                         else 'Discord requires one to four distinct configured slots')

    def refuse_over_capacity():
        # One colour per toolhead (#346): more distinct source colours than
        # toolheads can never be assigned; refuse before asking.
        colours = distinct_source_colours(manifest)
        if len(colours) > job_adapter.tool_count:
            raise printers.MultiColorUnsupported(
                f"{job_adapter.label} has {job_adapter.tool_count} toolheads and prints one color "
                f"per toolhead; this source uses {len(colours)} colors. Reduce the source to at "
                f"most {job_adapter.tool_count} colors or use a printer with more toolheads")

    async def ask(question, choices):
        from .link_intake import CANCEL_LABEL, PromptCancelled
        prompt, visible = _visible_choices(question, choices)
        offered = visible + [CANCEL_LABEL]
        cid = uuid.uuid4().hex
        cm.register(cid, session_key, prompt, offered)
        try:
            result = await adapter.send_clarify(chat_id=str(chat_id), question=prompt,
                choices=offered, clarify_id=cid, session_key=session_key, metadata=metadata)
            if not result.success:
                raise RuntimeError('Discord color prompt delivery failed')
            answer = await asyncio.to_thread(cm.wait_for_response, cid, timeout)
            if answer == CANCEL_LABEL:
                raise PromptCancelled('cancelled')
            if answer not in choices:
                raise RuntimeError('Discord color choice cancelled, expired or noncanonical; no decisions saved')
            return choices.index(answer)
        finally:
            # Cleanup only this prompt, never another pending gateway question.
            cm.resolve_gateway_clarify(cid, cm.CANCELLED)
            cm.wait_for_response(cid, 0.001)

    async def ask_one(row, taken):
        """Ask one colour question. On a multi_tool printer a toolhead already
        assigned a different source colour is not offered again (#346)."""
        if not multi_tool:
            return selections[await ask(row['question'], labels)]['slot']
        colour = prompt_source_colour(manifest, row)
        offered = [(label, sel) for label, sel in zip(labels, selections)
                   if taken.get(sel['slot'], colour) == colour]
        slot = offered[await ask(row['question'], [label for label, _ in offered])][1]['slot']
        taken[slot] = colour
        return slot

    async def intake_with_warning_round(**kwargs):
        """intake_color_choices with the filament warn-and-accept round.

        Filament findings (unknown usage, a damaged usage claim) no longer
        refuse the color flow (owner ruling, catduck, 2026-11-10): they are
        presented once, and only an explicit acceptance — recorded bound to
        the exact warnings, the source and the selected plates — lets the
        same intake run again and save the choices. Cancelling the warning
        question cancels the flow (ask raises PromptCancelled) and saves
        nothing.
        """
        try:
            intake_color_choices(path, state, **kwargs)
        except FilamentWarningPending as pending:
            await ask('Filament warnings for this print:\n'
                      + '\n'.join('- ' + w for w in pending.warnings)
                      + '\nAccept these warnings and save the color choices?',
                      ['Accept the warnings and continue'])
            record_filament_warning_acknowledgement(
                path, warnings=pending.warnings, source_sha256=pending.source_sha256,
                selected_plates=pending.selected_plates, via='discord_color_choices')
            intake_color_choices(path, state, **kwargs)

    async def ask_slots():
        # Numbered siblings (Stencil 1/2/3) share one question; answers expand back to canonical order.
        taken = {}
        if schema != 'part_slots_v1':
            return [await ask_one(row, taken) for row in questions]
        picked = {}
        for row in grouped_prompts(manifest, selected_plate_ids=selected_plates):
            slot = await ask_one(row, taken)
            for decision_key in row['keys']:
                picked[decision_key] = slot
        return [picked[row['key']] for row in questions]

    if job.get('color_decisions'):
        validate_decisions(manifest, job['color_decisions'], state, resolve_filament_selection,
                           selected_plate_ids=selected_plates)
        if (schema != 'part_slots_v1' or job['color_decisions'].get('mode') != 'per_part'):
            raise ValueError('source-bound color decisions already exist; refusing to replace or replay them')
        old_decisions = job['color_decisions']
        if multi_tool:
            refuse_over_capacity()
        slots = await ask_slots()
        latest = json.loads(path.read_text())
        if inventory(latest['downloaded_file'])['source_sha256'] != source_hash:
            raise ValueError('source changed during Discord selection; no decisions saved')
        if latest.get('color_decisions') != old_decisions:
            raise ValueError('concurrent color decision update; no decisions overwritten')
        if (latest.get('artifact_binding') != job.get('artifact_binding') or
                latest.get('derived_plates') != job.get('derived_plates') or
                latest.get('gcode') != job.get('gcode')):
            raise ValueError('derived artifact changed during Discord selection; no decisions overwritten')
        await intake_with_warning_round(mode='per_part', slots=slots)
        updated = json.loads(path.read_text())
        decisions = updated.get('color_decisions')
        validate_decisions(manifest, decisions, state, resolve_filament_selection,
                           selected_plate_ids=selected_plates)
        if decisions_digest(old_decisions) == decisions_digest(decisions):
            # Restore the original job atomically; intake_color_choices is the
            # canonical validator, but the user did not make a change.
            tmp = path.with_suffix(path.suffix + '.tmp')
            tmp.write_text(json.dumps(latest, indent=2) + '\n')
            __import__('os').replace(tmp, path)
            return {'status': 'COLOR_CHOICES_UNCHANGED', 'job_json': str(path),
                    'source_sha256': source_hash, 'mode': 'per_part',
                    'decisions': decisions, 'sliced': False, 'uploaded': False, 'started': False}
        set_color_decisions(job, decisions)
        write_job(path.parent, job)
        return {'status': 'COLOR_CHOICES_SAVED', 'job_json': str(path),
                'source_sha256': source_hash, 'mode': 'per_part', 'slots': slots,
                'decisions': decisions, 'sliced': False, 'uploaded': False, 'started': False}
    noun = 'stencils' if all('stencil' in str(p.get('name', '')).lower() for p in manifest['parts']) else 'parts'
    noun = 'stencils' if schema != 'part_slots_v1' else noun
    mode_index = await ask(
        f'How should the required {noun} be colored?',
        [f'Preserve colors per {noun[:-1]}/source region', f'All {noun} same color'])
    if mode_index == 1:
        slots = [selections[await ask(f'Choose the one color for all {noun}', labels)]['slot']]
        mode = 'single_color'
    else:
        if multi_tool:
            refuse_over_capacity()
        slots = await ask_slots()
        mode = 'per_part'
    latest = json.loads(path.read_text())
    if inventory(latest['downloaded_file'])['source_sha256'] != source_hash:
        raise ValueError('source changed during Discord selection; no decisions saved')
    if latest.get('color_decisions') != job.get('color_decisions'):
        raise ValueError('concurrent color decision update; no decisions overwritten')
    await intake_with_warning_round(mode=mode, slots=slots)
    saved = json.loads(path.read_text()).get('color_decisions')
    validate_decisions(manifest, saved, state, resolve_filament_selection,
                       selected_plate_ids=selected_plates)
    return {'status': 'COLOR_CHOICES_SAVED', 'job_json': str(path),
            'source_sha256': source_hash, 'mode': mode, 'slots': slots,
            'decisions': saved, 'sliced': False, 'uploaded': False, 'started': False}
