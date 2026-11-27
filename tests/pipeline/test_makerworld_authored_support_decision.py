"""MakerWorld author support intent must not turn a clearance note into a menu."""
import copy

import pytest

from print_handy.core import slice_warnings, support_clearance
from print_handy.intake.support_flow import supports_decision
from print_handy.launch.gates import require_current_artifact


@pytest.fixture
def elf_job():
    return {
        'site': 'makerworld', 'selected_source': {'instance_id': 4089960},
        'support_clearance': {'risky': True, 'findings': [{
            'code': support_clearance.CODE_CLEARANCE, 'severity': 'risk',
            'over': 'plate', 'trigger': 'area', 'sloped': False, 'part': 'Shorts', 'z_mm': {'min': 0.204},
            'area_mm2': 36, 'clearance_mm': {'min': 0.204, 'max': 0.204},
            'fixes': [{'fix': 'extend_underside'}, {'fix': 'sink'}],
        }]},
        'provenance': {
            'supports_on': True, 'support_decided': False,
            'slice_warnings': {'support_needed': False, 'support_texts': []},
            'conversion_provenance': {'settings': {'enable_support': {
                'project_value': '1', 'selected_value': '1',
                'selected_reason': 'designer', 'origin': 'designer',
            }}},
        },
    }


def test_elf_authored_supports_low_gap_is_not_a_question(elf_job):
    elf_job['artifact_binding'] = {'recipe_sha256': 'fixture'}
    with pytest.raises(RuntimeError, match='stale color artifact'):
        require_current_artifact(elf_job, None)
    finding = slice_warnings.support_finding(elf_job)
    show, reason, inputs = supports_decision(True, finding, elf_job['support_clearance'], job=elf_job)
    assert show is False
    assert inputs['clearance_risky'] is True
    assert 'designer' in reason


@pytest.mark.parametrize('value,origin', [('0', 'unknown'), ('1', 'inherited')])
def test_explicit_profile_value_even_without_preset_difference(elf_job, value, origin):
    setting = elf_job['provenance']['conversion_provenance']['settings']['enable_support']
    setting.update(project_value=value, selected_value=value, origin=origin)
    elf_job['provenance']['supports_on'] = value == '1'
    show, _, _ = supports_decision(True, None, elf_job['support_clearance'], job=elf_job)
    assert show is False


@pytest.mark.parametrize('variant', ['printables', 'no_profile', 'no_authored_setting', 'operator_override', 'warning', 'other_risk'])
def test_other_sources_and_unresolved_findings_still_ask(elf_job, variant):
    job = copy.deepcopy(elf_job)
    if variant == 'printables':
        job['site'] = 'printables'
    elif variant == 'no_profile':
        job['selected_source'] = {}
    elif variant == 'no_authored_setting':
        job['provenance']['conversion_provenance']['settings'] = {}
    elif variant == 'operator_override':
        job['provenance']['conversion_provenance']['settings']['enable_support']['selected_reason'] = 'operator'
    elif variant == 'warning':
        job['provenance']['slice_warnings']['support_needed'] = True
    else:
        job['support_clearance']['findings'].append({'code': 'OTHER', 'severity': 'risk'})
    show, _, _ = supports_decision(True, None, job['support_clearance'], job=job)
    assert show is True
    with pytest.raises(RuntimeError, match='insufficient support clearance'):
        require_current_artifact(job, None)
