"""A 3MF that names objects but carries no mesh is refused at intake (2026-11-03).

It used to pass the audit with only a geometry_error note and crash the fit check, which the
operator saw as "a problem it has no specific explanation for" (PH_INTAKE_UNKNOWN).
"""
import zipfile

import pytest

from print_handy.jobs import audit as jobs_audit
from print_handy.jobs import slice as jobs_slice

MODEL = ('<?xml version="1.0" encoding="UTF-8"?>'
         '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
         '<resources><object id="1" type="model"><mesh><vertices/><triangles/></mesh></object></resources><build><item objectid="1"/></build></model>')


def test_a_3mf_without_triangles_is_no_printable_volume(tmp_path):
    path = tmp_path / "empty.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", MODEL)
    with pytest.raises(jobs_slice.NoPrintableVolume, match="no printable geometry"):
        jobs_slice.model_info_output(path)
    audit = jobs_audit.audit(path)
    assert audit["no_printable_volume"] is True
