"""Exercise the MakerWorld Ready question with the elf low-gap evidence."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as S

from print_handy.core import prepared_intake, support_clearance
from print_handy.jobs import sources as jobs_sources, slice as jobs_slice, store as jobs_store
from tests.bound_names import setattr_bound
from .test_color_intake import state
from .test_intake_support_precheck import Adapter, env  # noqa: F401
from .test_makerworld_authored_support_decision import elf_job  # noqa: F401


def test_makerworld_profile_low_gap_ready_has_no_repair_menu(env, elf_job, monkeypatch):  # noqa: F811
    row = {'instance_id': 4089960, 'title': 'Elf shorts', 'plates': []}
    monkeypatch.setattr(jobs_sources, 'inventory_for_url', lambda url: {
        'site': 'makerworld', 'slug': 'elf', 'title': 'Elf shorts', 'sources': [row]})
    def download(job, chosen, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / 'elf.3mf'
        out.write_bytes(env.src.read_bytes())
        return out
    monkeypatch.setattr(jobs_sources, 'download_for_site', download)
    monkeypatch.setattr(support_clearance, 'safe_analyze', lambda *a, **kw: elf_job['support_clearance'])
    def sliced(job_path, recipe_sha256=None, **kwargs):
        job = json.loads(Path(job_path).read_text())
        job['provenance'] = elf_job['provenance']
        job['support_clearance'] = elf_job['support_clearance']
        gcode = Path(job_path).parent / 'sliced' / 'elf.gcode'
        gcode.parent.mkdir(parents=True, exist_ok=True)
        gcode.write_text('; enable_support = 1\nG1 X1\n')
        job['gcode'] = str(gcode)
        job['derived_plates'] = [{'gcode': str(gcode), 'plate_id': 1, 'slot': 1}]
        jobs_store.write_job(Path(job_path).parent, job)
        return [gcode]
    setattr_bound(monkeypatch, jobs_slice, 'slice_job', sliced)
    event = S(source=S(chat_id='c1', thread_id=None, user_id='u'), message_id='elf',
              text='https://makerworld.com/en/models/3572869-elf#profileId-4089960')
    adapter = Adapter([2, 0, 3])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    ready = next(q for q in adapter.questions if q['question'].startswith('Ready to slice'))
    assert ready['choices'][0] == 'Slice and print now'
    assert not any('support' in c.lower() or 'risk' in c.lower() or 'flatten' in c.lower()
                   or 'extend' in c.lower() for c in ready['choices'])
