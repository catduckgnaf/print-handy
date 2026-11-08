"""Declared part roles and assembly transforms on project_v1 components (#1017 item 3).

The manifest is the declaration surface: a source row may declare a part's
role and its placement in the assembly. Declarations are validated when the
manifest is built — a bad declaration raises instead of being dropped — and
stay absent (not invented) when no source declared them.
"""
import pytest

from print_handy.core import projects


def test_from_files_carries_declared_role_and_transform():
    rows = [
        {"name": "tray.stl", "downloaded_file": "/tmp/tray.stl",
         "role": "structural", "assembly_transform": {"translate_mm": [0, 0, 10]}},
        {"name": "ear.stl", "downloaded_file": "/tmp/ear.stl", "role": "rack_ear"},
        {"name": "plain.stl", "downloaded_file": "/tmp/plain.stl"},
    ]
    project = projects.from_files("intake-9", "uploaded_files", rows, "Rack")
    tray, ear, plain = project["components"]
    assert tray["role"] == "structural"
    assert tray["assembly_transform"] == {"translate_mm": [0.0, 0.0, 10.0],
                                          "rotate_deg": [0.0, 0.0, 0.0]}
    assert ear["role"] == "rack_ear" and "assembly_transform" not in ear
    assert "role" not in plain and "assembly_transform" not in plain


@pytest.mark.parametrize("role", ["structural", "coupon", "reference", "rack_ear"])
def test_every_declared_role_is_accepted(role):
    assert projects.validate_component_role(role) == role


@pytest.mark.parametrize("role", ["ear", "Rack Ear", "", None, 3, True])
def test_unknown_roles_raise_instead_of_being_dropped(role):
    with pytest.raises(ValueError):
        projects.validate_component_role(role)
    if role is not None:  # a null in a source row means "no declaration"
        with pytest.raises(ValueError):
            projects.from_files("intake-9", "uploaded_files",
                                [{"name": "a.stl", "role": role}])


def test_transform_normalizes_partial_declarations():
    assert projects.validate_assembly_transform({"translate_mm": [1, 2, 3]}) == {
        "translate_mm": [1.0, 2.0, 3.0], "rotate_deg": [0.0, 0.0, 0.0]}
    assert projects.validate_assembly_transform({"rotate_deg": [0, 0, 90]}) == {
        "translate_mm": [0.0, 0.0, 0.0], "rotate_deg": [0.0, 0.0, 90.0]}


@pytest.mark.parametrize("bad", [
    None, "10,0,0", [0, 0, 10], {"translate_mm": [0, 0]}, {"translate_mm": [0, 0, "10"]},
    {"translate_mm": [0, 0, True]}, {"translate_mm": [0, 0, float("nan")]},
    {"rotate_deg": [0, 0, float("inf")]}, {"scale": [1, 1, 1]},
    {"translate_mm": [0, 0, 0], "matrix": [1, 0, 0]},
])
def test_bad_transforms_raise_instead_of_being_dropped(bad):
    with pytest.raises(ValueError):
        projects.validate_assembly_transform(bad)
    if bad is not None:  # a null in a source row means "no declaration"
        with pytest.raises(ValueError):
            projects.from_files("intake-9", "uploaded_files",
                                [{"name": "a.stl", "assembly_transform": bad}])
