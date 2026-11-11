"""Collecting one project's files across several chat messages (#1204 item 2).

A project's parts do not always fit in one message. A person starts a
collection by sending files with "add to project" (or by typing "start a
project"), keeps sending parts, and presses **Done adding**. The collected
files then go through the exact intake an upload of several files in one
message uses (``prepared_intake.upload_job_set`` and the part chooser), so
this module adds an entry point, never a second combine engine.

Rules this module owns:

* A collection belongs to one scope: platform, chat, thread, user and the
  printer the channel serves. Another user's file, another thread, another
  printer's channel and any upload while no collection is open are never
  touched; they take the normal single-upload path exactly as before.
* A later upload joins only when the person says so: the upload's own text
  says "add to project", or they press **Add to project** on the question
  the upload gets. **Print it on its own** sends that upload through the
  normal intake, untouched by the collection.
* Every file is copied into the collection's own folder under a numbered
  sub-folder, so two files with the same name are two parts and neither
  overwrites the other.
* A file whose bytes equal a part already collected is not added again: a
  duplicate never silently becomes a second required part. Copies are a
  separate request, asked at print time.
* The collection lives in the bot's state folder beside the other per-chat
  state (``recent_model_links.json``), written atomically, so a restart
  keeps it. Buttons posted before a restart stop answering; typing
  "done adding" or sending the next part continues the same collection.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from ..hermes import _config

TTL_S = 7 * 24 * 3600
MAX_FILES = 30
STATE_NAME = 'upload_collections.json'
FILES_DIR = 'upload-collections'

ADD_LABEL = 'Add to project'
SOLO_LABEL = 'Print it on its own'
DONE_LABEL = 'Done adding'
CANCEL_PROJECT_LABEL = 'Cancel project'
NOT_NOW_LABEL = 'Cancel'

# "add to project", "add these to the project", "start a project", "collect the parts".
_START = re.compile(
    r"\b(?:add (?:this |these |it |them |that |those )?(?:file |files |part |parts )?to "
    r"(?:a |the |my |this )?project|start (?:a |new |the )?project|new project"
    r"|collect (?:these |the |my )?(?:parts|files))\b", re.I)
_DONE = re.compile(r"^\s*/?(?:done adding(?: (?:files|parts))?|finish (?:the )?project)\s*[.!]?\s*$", re.I)
_CANCEL = re.compile(r"^\s*/?(?:cancel (?:the |this |my )?project|stop collecting)\s*[.!]?\s*$", re.I)

_lock = threading.Lock()


def intent(text) -> str | None:
    """'start', 'done', 'cancel' or None for the message's text."""
    text = str(text or '')
    if _DONE.match(text):
        return 'done'
    if _CANCEL.match(text):
        return 'cancel'
    if _START.search(text):
        return 'start'
    return None


def scope_key(source, printer_id=None) -> str:
    """The one place a collection's owner is defined: platform, chat, thread, user, printer."""
    platform = getattr(getattr(source, 'platform', None), 'value', None) or 'discord'
    return json.dumps([str(platform), str(getattr(source, 'chat_id', '') or ''),
                       str(getattr(source, 'thread_id', '') or ''),
                       str(getattr(source, 'user_id', '') or ''), str(printer_id or '')])


def _state_path() -> Path:
    return _config.state_file(None, STATE_NAME)


def _files_root() -> Path:
    return _state_path().parent / FILES_DIR


def _load() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    rows = data.get('collections') if isinstance(data, dict) else None
    return {k: v for k, v in (rows or {}).items() if isinstance(v, dict)}


def _save(rows: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps({'schema': 'upload_collections_v1', 'collections': rows}, indent=1),
                   encoding='utf-8')
    os.replace(tmp, path)


def _live(rows: dict, now: float) -> dict:
    """Unexpired collections; an expired one's folder is removed with it."""
    out = {}
    for key, row in rows.items():
        try:
            fresh = now - float(row.get('updated_at', 0)) < TTL_S
        except (TypeError, ValueError):
            fresh = False
        if fresh:
            out[key] = row
        else:
            _remove_folder(row.get('collection_id'))
    return out


def _remove_folder(collection_id) -> None:
    if isinstance(collection_id, str) and re.fullmatch(r'[0-9a-f]{32}', collection_id):
        shutil.rmtree(_files_root() / collection_id, ignore_errors=True)


def current(scope: str, now=None) -> dict | None:
    """The open collection of this scope, or None."""
    now = time.time() if now is None else now
    with _lock:
        return _live(_load(), now).get(scope)


def _sha256(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        while chunk := handle.read(1 << 16):
            digest.update(chunk)
    return digest.hexdigest()


def display_name(path) -> str:
    """A cached upload's name without the gateway's doc_<12 hex>_ prefix."""
    return re.sub(r'^doc_[0-9a-f]{12}_', '', Path(str(path)).name)


def add(scope: str, paths, *, message_id=None, now=None) -> dict:
    """Open the scope's collection if needed and add these files to it.

    Returns ``{'collection': row, 'added': [...], 'duplicates': [(name, of_name)],
    'refused': [name, ...]}``. A refused file was over the file limit.
    """
    now = time.time() if now is None else now
    with _lock:
        rows = _live(_load(), now)
        row = rows.get(scope)
        if row is None:
            row = {'collection_id': uuid.uuid4().hex, 'scope': json.loads(scope),
                   'created_at': now, 'files': []}
        folder = _files_root() / row['collection_id']
        added, duplicates, refused = [], [], []
        for path in paths:
            name = display_name(path)
            sha = _sha256(path)
            same = next((f for f in row['files'] if f.get('sha256') == sha), None)
            if same is not None:
                duplicates.append((name, same['name']))
                continue
            if len(row['files']) >= MAX_FILES:
                refused.append(name)
                continue
            n = 1 + max((int(f.get('n', 0)) for f in row['files']), default=0)
            target = folder / str(n) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            entry = {'n': n, 'name': name, 'path': str(target), 'sha256': sha,
                     'size': target.stat().st_size, 'message_id': str(message_id or ''), 'added_at': now}
            row['files'].append(entry)
            added.append(entry)
        row['updated_at'] = now
        rows[scope] = row
        _save(rows)
        return {'collection': row, 'added': added, 'duplicates': duplicates, 'refused': refused}


def take(scope: str, collection_id=None, now=None) -> dict | None:
    """Close the scope's collection and return it, once: a second Done finds nothing.

    With ``collection_id``, only that collection is taken (a button from an
    older collection never takes a newer one). The files stay on disk until
    the collection folder expires, so the intake can still copy them.
    """
    now = time.time() if now is None else now
    with _lock:
        rows = _live(_load(), now)
        row = rows.get(scope)
        if row is None or (collection_id is not None and row.get('collection_id') != collection_id):
            return None
        missing = [f['name'] for f in row.get('files', []) if not Path(f.get('path', '')).is_file()]
        if missing:
            raise FileNotFoundError(', '.join(missing))
        del rows[scope]
        row['closed_at'] = now
        _save(rows)
        _write_closed_marker(row)
        return row


def _write_closed_marker(row) -> None:
    """Keep the taken collection's file list beside its files, for the expiry sweep."""
    folder = _files_root() / row['collection_id']
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'collection.json').write_text(json.dumps(row, indent=1), encoding='utf-8')
    except OSError:
        pass


def cancel(scope: str, now=None) -> dict | None:
    """Discard the scope's collection and its copied files; returns what was dropped."""
    now = time.time() if now is None else now
    with _lock:
        rows = _live(_load(), now)
        row = rows.pop(scope, None)
        if row is None:
            return None
        _save(rows)
    _remove_folder(row.get('collection_id'))
    return row


def sweep(now=None) -> None:
    """Remove folders of taken collections older than the expiry. Never raises."""
    now = time.time() if now is None else now
    try:
        with _lock:
            open_ids = {row.get('collection_id') for row in _live(_load(), now).values()}
        for folder in _files_root().iterdir():
            if folder.name in open_ids or not folder.is_dir():
                continue
            if now - folder.stat().st_mtime > TTL_S:
                _remove_folder(folder.name)
    except OSError:
        pass


def listing(row) -> str:
    files = row.get('files') or []
    return '\n'.join(f"{i}. {f['name']}" for i, f in enumerate(files, 1))


def status_text(result) -> str:
    """What the chat hears after files were added (or offered and refused)."""
    row = result['collection']
    files = row.get('files') or []
    lines = []
    if result['added']:
        names = ', '.join(f['name'] for f in result['added'])
        lines.append(f'Added {names} to the project.')
    for name, of in result['duplicates']:
        lines.append(f'{name} has exactly the same bytes as {of}, which is already in the project, so it '
                     'was not added as a second part. Ask for copies when you print if you need more.')
    if result['refused']:
        lines.append(f"Not added: {', '.join(result['refused'])}. A project collects at most {MAX_FILES} files.")
    if files:
        lines.append(f"The project has {len(files)} file{'s' if len(files) != 1 else ''}:\n{listing(row)}")
    else:
        lines.append('The project has no files yet.')
    lines.append(f'Send more parts here, then press {DONE_LABEL} (or type "done adding"). '
                 'Nothing is sliced or started until then.')
    return '\n'.join(lines)


def join_question(row, names) -> str:
    files = row.get('files') or []
    return (f"You have a project collecting files here ({len(files)} so far). Add "
            f"{', '.join(names)} to it, or print {'it' if len(names) == 1 else 'them'} on "
            f"{'its' if len(names) == 1 else 'their'} own?")


async def handle(event, adapter, *, printer_id, files, run_intake, timeout=None, live_key=None, ask=None,
                 archives=()):
    """The collection's part of one message, or None when the collection is not involved.

    ``files`` are the message's STL/3MF uploads and ``archives`` its ZIPs
    (a ZIP is a project by itself, never collected); ``run_intake(paths, row)``
    runs the normal intake on the collected files and returns its result.
    None means: this message is not about a collection, handle it exactly
    as before.
    """
    source = event.source
    said = intent(getattr(event, 'text', '') or '')
    if said is None and not files:
        return None
    scope = scope_key(source, printer_id)
    open_row = current(scope)
    if said is None and open_row is None:
        return None  # an ordinary upload: the collection never sees it
    metadata = {'thread_id': source.thread_id} if getattr(source, 'thread_id', None) else None
    if ask is None:
        from .link_intake import ask as _ask

        async def ask(question, choices):
            return await _ask(adapter, source, 'upload-collection:' + scope, question, choices,
                              timeout, live_key=live_key)

    async def say(text):
        await adapter.send(chat_id=source.chat_id, content=text, metadata=metadata)

    if said == 'start' and archives:
        await say('A ZIP is not collected into a project: send it in its own message, without '
                  '"add to project", and its printable parts become one project by themselves. '
                  'Nothing was added, sliced or started.')
        return {'action': 'skip', 'reason': 'archive not collected'}
    if said == 'cancel':
        dropped = cancel(scope)
        if dropped is None:
            await say('No project is collecting files here. Nothing was changed.')
            return {'action': 'skip', 'reason': 'no upload collection to cancel'}
        count = len(dropped.get('files') or [])
        await say(f"Project cancelled; the {count} collected file{'s' if count != 1 else ''} "
                  'were discarded. Nothing was sliced or started.')
        return {'action': 'skip', 'reason': 'upload collection cancelled'}
    if said == 'done':
        if open_row is None:
            await say('No project is collecting files here. Send files with "add to project" to start one.')
            return {'action': 'skip', 'reason': 'no upload collection to finish'}
        return await _finish(scope, open_row['collection_id'], say, run_intake)
    if open_row is not None and said is None:
        names = [display_name(p) for p in files]
        from .link_intake import PromptCancelled, PromptExpired
        try:
            pick = await ask(join_question(open_row, names), [ADD_LABEL, SOLO_LABEL, NOT_NOW_LABEL])
        except (PromptCancelled, PromptExpired):
            pick = 2
        if pick == 1:
            return None  # the normal single-upload intake takes this message, unchanged
        if pick != 0:
            await say('Not added to the project and not printed. Nothing was sliced or started.')
            return {'action': 'skip', 'reason': 'upload not added to the collection'}
    result = add(scope, files, message_id=getattr(event, 'message_id', None))
    row = result['collection']
    from .link_intake import PromptCancelled, PromptExpired
    try:
        pick = await ask(status_text(result), [DONE_LABEL, CANCEL_PROJECT_LABEL])
    except PromptExpired:
        return {'action': 'skip', 'reason': 'upload collection open'}
    except PromptCancelled:
        pick = 1
    if pick == 0:
        return await _finish(scope, row['collection_id'], say, run_intake)
    if cancel(scope) is not None:
        await say('Project cancelled; the collected files were discarded. Nothing was sliced or started.')
    return {'action': 'skip', 'reason': 'upload collection cancelled'}


async def _finish(scope, collection_id, say, run_intake):
    try:
        row = take(scope, collection_id)
    except FileNotFoundError as exc:
        await say(f'These collected files are missing from disk: {exc}. The project stays open; '
                  'send them again or type "cancel project". Nothing was sliced or started.')
        return {'action': 'skip', 'reason': 'upload collection files missing'}
    if row is None:
        await say('That project was already finished or cancelled. Nothing new was started.')
        return {'action': 'skip', 'reason': 'upload collection already taken'}
    files = row.get('files') or []
    if not files:
        await say('The project has no files, so there is nothing to prepare. Nothing was sliced or started.')
        return {'action': 'skip', 'reason': 'empty upload collection'}
    sweep()
    await say(f"Done adding. Preparing the project from {len(files)} file{'s' if len(files) != 1 else ''}.")
    return await run_intake([f['path'] for f in files], row)
