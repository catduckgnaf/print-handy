"""Chat model edits and model questions (#903), moved out of the
print-feedback plugin. The plugin keeps a thin ``edit_dispatch`` that
passes its own module namespace as the host, so this module reuses the
plugin's gate, confirm, state and design helpers without importing the
plugin file.
"""
import asyncio
import contextvars
import copy
import json
import logging
import re
import time
from pathlib import Path

from print_handy.hermes import _config

logger = logging.getLogger(__name__)

_HOST = contextvars.ContextVar('print_handy_edit_chat_host')


def _h():
    """The plugin namespace that started the current dispatch."""
    return _HOST.get()


async def edit_dispatch(event, gateway, *, host, **kwargs):
    """Entry point: bind the plugin host for this dispatch, then run it."""
    token = _HOST.set(host)
    try:
        return await _dispatch(event, gateway, **kwargs)
    finally:
        _HOST.reset(token)


async def follow_up(event, gateway, source, adapter, instruction, *, host):
    """Apply an edit or answer a question carried in the same message as a
    model link or file, after the intake prepared the new job (#903)."""
    token = _HOST.set(host)
    try:
        return await _follow_up(event, gateway, source, adapter, instruction)
    finally:
        _HOST.reset(token)


# ------------------------------------------------------------------ chat model edits (#903)
#
# One verified edit-plan pipeline: a chat edit request resolves through
# the deterministic vocabulary in core/edit_plan.py (fuzzy, confirm
# first — never an action on a guess), or, when the vocabulary cannot
# resolve it, the AI lane proposes a plan from the same vocabulary (or a
# geometry route into the design engine). The plan is probe-sliced and
# verified BEFORE the job is touched; only a verified edit re-enters the
# intake flow, which re-slices and re-asks every approval against the
# new G-code hash. Authorization follows the channel's normal access;
# the AI lane is further gated by ai.allowed_users and the per-user
# hourly limit (#902, rescoped). A denied AI lane still leaves the
# vocabulary working.

_EDIT_UNDO_RE = re.compile(
    r"^\s*(undo|undo (the )?last edit|take back (the )?last edit|revert (the )?last edit)\s*[.!]?\s*$",
    re.IGNORECASE)


def _edit_current_job(root, source):
    """(receipt, job_path, job) for the newest live intake in this
    channel/thread, or None. The job the user last prepared here is the
    job an edit applies to."""
    best = None
    for path in Path(root).glob('*.json'):
        try:
            receipt = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if str(receipt.get('chat_id')) != str(source.chat_id):
            continue
        receipt_thread = receipt.get('thread_id')
        source_thread = getattr(source, 'thread_id', None)
        if (str(receipt_thread) if receipt_thread else None) != \
                (str(source_thread) if source_thread else None):
            continue
        if receipt.get('status') in ('CANCELLED', 'CANCELLED_OVERSIZED', 'EXPIRED',
                                     'DECLINED_REPRINT', 'INTERRUPTED'):
            continue
        job_json = receipt.get('job_json')
        if not job_json or not Path(job_json).is_file():
            continue
        when = path.stat().st_mtime
        if best is None or when > best[0]:
            best = (when, receipt, Path(job_json))
    if best is None:
        return None
    try:
        job = json.loads(best[2].read_text())
    except (OSError, ValueError):
        return None
    return best[1], best[2], job


def _edit_is_multipart(job):
    audit = job.get('audit') or {}
    decisions = job.get('color_decisions') or {}
    return int(audit.get('parts') or 1) > 1 or decisions.get('mode') == 'per_part'


def _edit_probe(job, plan):
    """Slice a throwaway copy of the job with the plan applied and verify
    the result. Returns (ok, lines). The real job is never touched."""
    import shutil
    import tempfile
    from print_handy.core import edit_plan, model_job as mj
    scratch = Path(tempfile.mkdtemp(prefix='print-handy-edit-probe-'))
    try:
        probe_dir = scratch / 'job'
        probe_dir.mkdir()
        probe = copy.deepcopy(job)
        source = Path(job.get('downloaded_file') or '')
        if not source.is_file():
            return False, ['FAILED: the model file for this job is no longer on disk']
        local = probe_dir / source.name
        shutil.copyfile(source, local)
        probe['downloaded_file'] = str(local)
        if probe.get('sources'):
            probe['sources'] = [dict(probe['sources'][0], path=str(local), name=source.name)]
        edit_plan.apply_plan(probe, plan)
        probe_path = probe_dir / 'JOB.json'
        mj.write_job(probe_dir, probe)
        try:
            outputs = mj.slice_job(str(probe_path))
        except Exception as exc:
            return False, [f'FAILED: the edited slice did not run: {exc}']
        if not outputs:
            return False, ['FAILED: the edited slice produced no G-code']
        text = Path(outputs[0]).read_text(errors='ignore')
        return edit_plan.verify_slice(plan, gcode_text=text,
                                      before_dims=edit_plan.job_dims(job))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


async def _edit_question(event, gateway, source, adapter, kind):
    """Answer a question about the current job (#903) from its slice result and
    mesh; a supports question on an unsupported slice offers the #904 choice."""
    from print_handy.core import model_questions
    from print_handy.core import model_job as mj
    from print_handy.core.prepared_intake import PRINT_NO_SUPPORTS_LABEL, RESLICE_SUPPORTS_LABEL
    metadata = {'thread_id': source.thread_id} if source.thread_id else None

    async def say(content):
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=content)

    found = _edit_current_job(_h()._state_dir() / 'model-link-intakes', source)
    if found is None:
        return None  # no job here: the question is not about a model, leave it to chat
    _receipt, job_path, job = found
    if kind == 'size':
        await say(model_questions.size_answer(job))
        return {'action': 'skip', 'reason': 'question answered: size'}
    if kind == 'fit':
        from print_handy import settings
        await say(model_questions.fit_answer(job, settings.current()['printer']['bed_mm']))
        return {'action': 'skip', 'reason': 'question answered: fit'}
    model = job.get('downloaded_file')
    answer, offer = model_questions.supports_answer(job, model if model and Path(model).is_file() else None)
    if not offer:
        await say(answer)
        return {'action': 'skip', 'reason': 'question answered: supports'}
    timeout = _config.question_timeout_s()
    pick = await _h()._confirm(adapter, source, metadata, 'model-question', answer + '\nWhat should I do?',
                          [RESLICE_SUPPORTS_LABEL, PRINT_NO_SUPPORTS_LABEL], timeout,
                          live_key=gateway._session_key_for_source(source))
    if pick == RESLICE_SUPPORTS_LABEL:
        mj.record_support_decision(job_path.parent, True)
        job = json.loads(job_path.read_text())
        await say('Re-slicing with supports.')
        await _edit_reenter(event, gateway, source, adapter, job)
        return {'action': 'skip', 'reason': 'question answered: re-slice with supports'}
    if pick == PRINT_NO_SUPPORTS_LABEL:
        mj.record_support_decision(job_path.parent, False)
        await say('Recorded: print without supports. The slice is unchanged.')
        return {'action': 'skip', 'reason': 'question answered: print without supports'}
    await say('No change made.')
    return {'action': 'skip', 'reason': 'question answered: no choice'}


async def _edit_reenter(event, gateway, source, adapter, job):
    """Re-enter the intake flow with the edited job as a resume: the flow
    re-slices it, shows the new summary and preview, and asks every
    approval fresh against the new G-code hash."""
    from print_handy.core import printer_state
    from print_handy.core.prepared_intake import prepare_link, resume_job
    resume = resume_job(job)
    for field in ('scale', 'copies', 'orientation', 'fit_brim_policy', 'edit_settings',
                  'edits', 'printer_id', 'unit_decision', 'support_decision'):
        if job.get(field) is not None:
            resume[field] = job[field]
    root = _h()._state_dir() / 'model-link-intakes'
    pid = job.get('printer_id') or _h()._routed_printer_id(source, gateway)
    return await prepare_link(event, adapter, printer_state.load(**_h()._pkw(pid)), root,
                              reuse=resume, **_h()._pkw(pid))


def _edit_new_job(root, source, event):
    """The job record the re-entered intake just wrote for this event."""
    from print_handy.core import model_job as mj
    try:
        iid = mj.intake_id_from_message(source, getattr(event, 'message_id', None))
        receipt = json.loads((Path(root) / (iid + '.json')).read_text())
        job_path = Path(receipt['job_json'])
        return job_path, json.loads(job_path.read_text())
    except Exception:
        return None, None


async def _edit_finish_record(root, source, event, *, verification):
    """Complete the provenance record on the NEW job (the resume carried
    it over) with the verification result and the new G-code's hash."""
    from print_handy.core import edit_plan, model_job as mj
    job_path, new_job = _edit_new_job(root, source, event)
    if new_job is None:
        return
    gcode = new_job.get('gcode')
    edit_plan.finish_edit_record(
        new_job, verification=verification,
        output_sha256=edit_plan.file_sha256(gcode) if gcode else None)
    mj.write_job(job_path.parent, new_job)


async def _edit_apply(event, gateway, source, adapter, job_path, job, plan, *, lane):
    """Probe-verify, record, apply and re-enter one confirmed plan."""
    from print_handy.core import edit_plan, model_job as mj
    metadata = {'thread_id': source.thread_id} if source.thread_id else None

    async def say(content):
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=content)

    ok, lines = await asyncio.to_thread(_edit_probe, job, plan)
    if not ok:
        await say('That edit did not verify, so nothing was changed:\n'
                  + '\n'.join(f'- {line}' for line in lines))
        return {'action': 'skip', 'reason': 'edit verification failed'}
    actor = f"{_h()._platform_value(source) or 'discord'}:{getattr(source, 'user_id', '')}"
    source_path = job.get('downloaded_file')
    original = copy.deepcopy(job)  # restored verbatim if re-entry fails
    record = edit_plan.record_edit(job, plan, actor=actor, lane=lane,
                                   source_sha256=edit_plan.file_sha256(source_path))
    edit_plan.apply_plan(job, plan)
    mj.write_job(job_path.parent, job)
    try:
        result = await _edit_reenter(event, gateway, source, adapter, job)
    except Exception as exc:
        # The message below says the job is unchanged, so make that
        # true: write the pristine record back over the applied edit
        # (whose provenance record goes with it — the attempt is in
        # the log, and a failed edit must not steer later edits/undo).
        mj.write_job(job_path.parent, original)
        logger.warning('edit re-entry failed after a verified probe slice: %r', exc)
        await say('The edit verified on a test slice, but re-slicing the job for presentation '
                  'failed, so the job above is unchanged. Nothing was started.')
        return {'action': 'skip', 'reason': 'edit re-entry failed'}
    await _edit_finish_record(_h()._state_dir() / 'model-link-intakes', source, event,
                              verification=lines)
    await say('Edit applied and verified:\n' + '\n'.join(f'- {line}' for line in lines))
    return result if isinstance(result, dict) else {'action': 'skip', 'reason': 'edit applied'}


async def _edit_undo(event, gateway, source, adapter, job_path, job):
    from print_handy.core import edit_plan, model_job as mj
    metadata = {'thread_id': source.thread_id} if source.thread_id else None

    async def say(content):
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=content)

    if not (job.get('edits')):
        await say('There is no edit to undo on the current job.')
        return {'action': 'skip', 'reason': 'nothing to undo'}
    actor = f"{_h()._platform_value(source) or 'discord'}:{getattr(source, 'user_id', '')}"
    undone = edit_plan.undo_last_edit(job, actor=actor)
    if undone is None:
        await say('Every edit on the current job has already been undone.')
        return {'action': 'skip', 'reason': 'nothing to undo'}
    mj.write_job(job_path.parent, job)
    result = await _edit_reenter(event, gateway, source, adapter, job)
    await _edit_finish_record(_h()._state_dir() / 'model-link-intakes', source, event,
                              verification=['undo: the restored state was re-sliced by the '
                                            'intake flow'])
    await say(f"Undid the last edit ({edit_plan.describe_plan(undone['plan'])}). "
              'The restored job was re-sliced above; approvals are asked again for the new G-code.')
    return result if isinstance(result, dict) else {'action': 'skip', 'reason': 'edit undone'}


_EDIT_AI_CALLS = {}
_MENTION_RE = re.compile(r'<(?:@[!&]?|#)\d+>|@(?:everyone|here)\b')


def _sanitize_mentions(text):
    """Strip Discord user, role and channel mentions and @everyone/@here (#902)."""
    return re.sub(r'\s{2,}', ' ', _MENTION_RE.sub('', str(text or ''))).strip()


def _edit_ai_allowed(source, now=None):
    """(allowed, reason) for the AI edit lane: the [ai] allowed_users list and the
    per-user edit_requests_per_hour limit (#902). A refused request falls back to
    the vocabulary; nothing is sent to the endpoint."""
    from print_handy.core import edit_plan
    try:
        from print_handy import settings
        ai = settings.current().get('ai') or {}
    except Exception:
        ai = {}
    return edit_plan.ai_lane_allowed(getattr(source, 'user_id', ''), ai, _EDIT_AI_CALLS,
                                     time.time() if now is None else now)


# Short per-user, per-channel memory for the AI edit lane (#902, rescoped):
# the last few exchanges, so "make it stronger" can follow "thicker walls?".
MEMORY_TURNS = 3
MEMORY_TTL_S = 30 * 60
_EDIT_MEMORY = {}


def _memory_key(source):
    return (str(getattr(source, 'chat_id', '')), getattr(source, 'thread_id', None) or None,
            str(getattr(source, 'user_id', '')))


def _recall(key, now=None):
    now = time.time() if now is None else now
    turns = [t for t in _EDIT_MEMORY.get(key, []) if now - t[0] <= MEMORY_TTL_S]
    if turns:
        _EDIT_MEMORY[key] = turns
    else:
        _EDIT_MEMORY.pop(key, None)
    return turns


def _remember(key, prompt, reply, now=None):
    now = time.time() if now is None else now
    turns = _recall(key, now) + [(now, str(prompt), str(reply))]
    _EDIT_MEMORY[key] = turns[-MEMORY_TURNS:]


async def _edit_ai_reply(cfg, source, prompt):
    """One AI-lane answer for an edit request through the configured
    design/LLM HTTP endpoint. Returns the reply text, or None when no
    endpoint resolves or the user may not use the AI lane."""
    from print_handy.core import design
    if design.resolve_http_config(cfg) is None:
        return None
    allowed, reason = _edit_ai_allowed(source)
    if not allowed:
        logger.info('edit AI lane refused for %s: %s', getattr(source, 'user_id', '?'), reason)
        return None
    key = _memory_key(source)
    messages = []
    for _, said, answered in _recall(key):
        messages += [{'role': 'user', 'content': said}, {'role': 'assistant', 'content': answered}]
    messages.append({'role': 'user', 'content': prompt})
    reply = await asyncio.to_thread(design.http_complete, cfg, messages)
    if reply:
        _remember(key, prompt, reply)
    return reply


async def _edit_geometry_route(event, gateway, source, adapter, job, instruction):
    """Route a geometry change into the design engine (#388) — refused
    up front for a multi-part 3MF, whose per-part assignments a mesh
    edit would silently flatten."""
    from print_handy.core import design, edit_plan, geometry_layout, orca_adapter
    metadata = {'thread_id': source.thread_id} if source.thread_id else None

    async def say(content):
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=content)

    if _edit_is_multipart(job):
        await say('That change edits the model geometry itself, and this is a multi-part '
                  'model: a geometry edit would flatten it to a single-color mesh and lose '
                  'the per-part color assignments, so I will not do it silently. Job edits '
                  '(supports, scale, rotation, copies, brim, layer height, infill) keep every '
                  'assignment — or split the model and edit one part at a time.')
        return {'action': 'skip', 'reason': 'geometry edit refused for multi-part model'}
    source_path = Path(job.get('downloaded_file') or '')
    if not source_path.is_file():
        await say('The model file for this job is no longer on disk, so I cannot edit its geometry.')
        return {'action': 'skip', 'reason': 'geometry edit: source file missing'}
    cfg = _config.cfg()
    complete, _kind = _h()._design_transport(cfg)
    if complete is None:
        await say('That change needs the design model, and no design endpoint is configured '
                  '— see docs/DESIGN.md. Nothing was changed.')
        return {'action': 'skip', 'reason': 'geometry edit: no design endpoint'}
    engine = design.DesignEngine((cfg.get('paths') or {}).get('cache') or (_h()._hermes_home() / 'cache'),
                                 complete, cfg=cfg)
    owner = f"{source.chat_id}:{source.thread_id or ''}:{getattr(source, 'user_id', '')}"
    try:
        before = geometry_layout.describe_triangles(orca_adapter.source_triangles(source_path))
        session = await asyncio.to_thread(engine.start_edit_session, owner, source.chat_id,
                                          instruction, source_path)
        result = await engine.run_turn(session, instruction)
        after = design.validate_stl(result['stl_path'])
        if not edit_plan.finite_triangles(orca_adapter.read_stl_triangles(result['stl_path'])):
            raise design.DesignError('the edited mesh has non-finite coordinates, '
                                     'so there is nothing safe to print')
    except design.DesignError as exc:
        await say(f'That design change did not work: {exc} Nothing was sliced or started.')
        return {'action': 'skip', 'reason': 'geometry edit failed'}
    delta = ''
    if before.get('dimensions_mm') and after.get('dimensions_mm'):
        fmt = lambda dims: ' x '.join(f'{float(v):g}' for v in dims)
        delta = (f" Geometry check: watertight, fits are re-checked by the intake; size "
                 f"{fmt(before['dimensions_mm'])} mm → {fmt(after['dimensions_mm'])} mm, "
                 f"volume {float(before.get('abs_volume_mm3') or 0):,.0f} → "
                 f"{float(after.get('abs_volume_mm3') or 0):,.0f} mm³.")
    await say(f"**Design turn {result['session']['turns']}** — {design.summary_text(result)}{delta}")
    timeout = _config.question_timeout_s()
    answer = await _h()._confirm(adapter, source, metadata, 'print-design',
                            'What next for this design?', list(design.CHOICES), timeout,
                            live_key=gateway._session_key_for_source(source))
    if answer == design.SLICE_LABEL:
        engine.mark_handed_off(result['session'])
        return await _h()._design_handoff(event, gateway, adapter, result['session'],
                                     result['stl_path'])
    if answer == design.CHANGE_LABEL:
        engine.awaiting_revision(result['session'])
        await say('Say the change in your next message.')
        return {'action': 'skip', 'reason': 'geometry edit awaiting revision'}
    engine.cancel(result['session'])
    await say('Design cancelled. Nothing was sliced or started.')
    return {'action': 'skip', 'reason': 'geometry edit cancelled'}


def instruction_after_model(text, url=None):
    """The edit or question carried beside a model link or file in one
    message ("<link> shrink this by 20%"), or None when the rest of the
    message is not one. Plain intake text ("print this") stays None."""
    rest = str(text or '')
    if url:
        rest = rest.replace(url, ' ')
    rest = re.sub(r'https?://\S+', ' ', rest).strip()
    if not rest:
        return None
    from print_handy.core import edit_plan, model_questions
    if model_questions.classify(rest) is not None:
        return rest
    try:
        if edit_plan.resolve_request(rest) is not None:
            return rest
    except edit_plan.PlanError:
        return rest
    return rest if edit_plan.looks_like_edit(rest) else None


async def _follow_up(event, gateway, source, adapter, instruction):
    """Run the instruction against the job the intake just prepared, through
    the same edit and question path a separate message would take."""
    import copy as _copy
    follow = _copy.copy(event)
    follow.text = instruction
    follow.media_urls = []
    follow.media_types = []
    follow.metadata = dict(getattr(event, 'metadata', None) or {})
    follow.message_id = f"{getattr(event, 'message_id', '') or 'msg'}:instruction"
    return await _dispatch(follow, gateway)


async def _dispatch(event, gateway, **kwargs):
    """Chat model edits (#903): vocabulary plans, AI proposals, undo —
    all confirmed, probe-verified, recorded, and re-approved."""
    source, adapter = _h()._control_gate(event, gateway)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'edit UI delivery unavailable'}
    from print_handy.core import edit_plan
    from print_handy.core.link_intake import model_link
    from print_handy.core.prepared_intake import model_file
    text = str(getattr(event, 'text', '') or '')
    if model_link(text) or model_file(event):
        return None  # a fresh intake owns links and files
    undo = bool(_EDIT_UNDO_RE.match(text.strip()))
    from print_handy.core import model_questions
    question_kind = None if undo else model_questions.classify(text)
    if question_kind is not None:
        return await _edit_question(event, gateway, source, adapter, question_kind)
    resolve_error = None
    try:
        resolved = None if undo else edit_plan.resolve_request(text)
    except edit_plan.PlanError as exc:
        # A typed request whose values fall outside the plan bounds
        # (infill 200%, brim 30 mm): answer with the rejection below
        # instead of aborting the dispatch on ordinary invalid input.
        resolved, resolve_error = None, exc
    if resolve_error is None and not undo and resolved is None \
            and not edit_plan.looks_like_edit(text):
        return None
    metadata = {'thread_id': source.thread_id} if source.thread_id else None

    async def say(content):
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=content)

    if resolve_error is not None:
        await say(f'I cannot make that edit: {resolve_error} Nothing was changed. '
                  + edit_plan.vocabulary_text())
        return {'action': 'skip', 'reason': 'edit request refused'}

    root = _h()._state_dir() / 'model-link-intakes'
    found = _edit_current_job(root, source)
    if found is None:
        await say('There is no current model job in this channel to edit. Send a model link '
                  'or drop the file first, and I will prepare it.')
        return {'action': 'skip', 'reason': 'edit: no current job'}
    _receipt, job_path, job = found
    if undo:
        return await _edit_undo(event, gateway, source, adapter, job_path, job)
    lane = 'fuzzy'
    plan = None
    if resolved is not None and resolved[0] == 'pick':
        timeout = _config.question_timeout_s()
        labels = [edit_plan.describe_plan(candidate) for candidate in resolved[1]]
        answer = await _h()._confirm(adapter, source, metadata, 'model-edit',
                                f"Which edit did you mean for {job.get('title') or 'the current model'}?",
                                labels + ['Cancel'], timeout,
                                live_key=gateway._session_key_for_source(source))
        if answer not in labels:
            await say('No edit made.')
            return {'action': 'skip', 'reason': 'edit pick cancelled'}
        plan = resolved[1][labels.index(answer)]
    elif resolved is not None:
        plan = resolved[1]
    if plan is None:
        # The vocabulary could not resolve it: the AI lane may propose a
        # plan from the same vocabulary, or route a geometry change.
        cfg = _config.cfg()
        prompt = edit_plan.ai_prompt(_sanitize_mentions(text), job)
        try:
            reply = await _edit_ai_reply(cfg, source, prompt)
        except Exception as exc:
            logger.warning('edit AI lane failed: %r', exc)
            reply = None
        if reply is None:
            await say('I could not match that to an edit I can make. ' + edit_plan.vocabulary_text())
            return {'action': 'skip', 'reason': 'edit vocabulary shown'}
        try:
            route, payload = edit_plan.parse_ai_reply(reply)
        except edit_plan.PlanError as exc:
            await say(f'The AI proposed an edit I cannot use: {exc} Nothing was changed. '
                      + edit_plan.vocabulary_text())
            return {'action': 'skip', 'reason': 'edit AI plan rejected'}
        if route == 'geometry':
            return await _edit_geometry_route(event, gateway, source, adapter, job, payload)
        if route == 'none':
            await say('That does not sound like a model edit. ' + edit_plan.vocabulary_text())
            return {'action': 'skip', 'reason': 'edit AI declined'}
        plan, lane = payload, 'ai'
    if plan['kind'] == 'copies' and _edit_is_multipart(job):
        await say('Copies only work for jobs that print on one plate in one color right now; '
                  'this job is multi-part, so nothing was changed.')
        return {'action': 'skip', 'reason': 'edit copies refused for multi-part job'}
    source_path = Path(job.get('downloaded_file') or '')
    try:
        plan = edit_plan.enrich_plan(plan, job, source_path if source_path.is_file() else None)
    except edit_plan.PlanError as exc:
        await say(f'I cannot make that edit: {exc} Nothing was changed.')
        return {'action': 'skip', 'reason': 'edit refused'}
    timeout = _config.question_timeout_s()
    answer = await _h()._confirm(adapter, source, metadata, 'model-edit',
                            f"Edit {job.get('title') or 'the current model'}: "
                            f"{edit_plan.describe_plan(plan)}?",
                            ['Yes, apply the edit', 'Cancel'], timeout,
                            live_key=gateway._session_key_for_source(source))
    if answer != 'Yes, apply the edit':
        await say('No edit made.')
        return {'action': 'skip', 'reason': 'edit declined'}
    return await _edit_apply(event, gateway, source, adapter, job_path, job, plan, lane=lane)
