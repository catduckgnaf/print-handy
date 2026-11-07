import asyncio,importlib.util,json,sys
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import AsyncMock,patch
import pytest
from .test_discord_color_intake import Transport
from .test_color_intake import source,state
from print_handy import settings
from print_handy.core import source_colors, model_job

PLUGIN = settings.ROOT/'plugins/print-feedback/__init__.py'


def load_plugin(name):
    spec=importlib.util.spec_from_file_location(name,PLUGIN)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    # The plugin ships color_selection_dispatch (plugins/print-feedback);
    # a missing hook is a regression, not a skip. (The hasattr skip guard
    # here predated the hook landing and was removed in the 2026-11-07
    # test-suite cleanup.)
    assert hasattr(mod,'color_selection_dispatch')
    return mod

@pytest.mark.asyncio
async def test_profile_hook_uses_real_discord_and_persists(tmp_path):
    mod=load_plugin('candidate_feedback')
    p=source(tmp_path);jp=tmp_path/'JOB.json'
    jp.write_text(json.dumps({'downloaded_file':str(p),'color_manifest':source_colors.inventory(p)}))
    adapter=Transport([0,0,0,1,0,1])  # first pick answers the new color mode question: preserve per stencil
    src=S(platform=S(value='discord'),profile='3dprinting',user_id='42',chat_id='offline',thread_id=None)
    event=S(source=src,text='/print-colors '+str(jp),internal=False)
    gateway=S(_is_user_authorized_for_source=lambda s:True,_delivery_adapter_for=lambda s:adapter)
    runtime_job = model_job
    with patch.object(runtime_job,'ARCHIVE',tmp_path),patch.object(runtime_job.printer_state,'load',return_value=state()):
        result=await mod.color_selection_dispatch(event,gateway)
    assert result['action']=='skip'
    assert len(adapter.questions)==5, adapter.sent
    assert json.loads(jp.read_text())['color_decisions']['mode']=='per_part'
    assert len(adapter.sent)==1

@pytest.mark.asyncio
async def test_hook_unauthorized_has_zero_transport(tmp_path):
    mod=load_plugin('candidate_feedback_denied')
    adapter=S(send=AsyncMock(),send_clarify=AsyncMock())
    src=S(platform=S(value='discord'),profile='3dprinting',user_id='intruder',chat_id='offline',thread_id=None)
    gateway=S(_is_user_authorized_for_source=lambda s:False,_delivery_adapter_for=lambda s:adapter)
    assert await mod.color_selection_dispatch(S(source=src,text='/print-colors /etc/JOB.json'),gateway) is None
    adapter.send.assert_not_called();adapter.send_clarify.assert_not_called()
