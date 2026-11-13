"""#1373: a full-size split that turned parts onto supports says so on the Ready question.

Found by the corpus on a 23-plate tray project: the split plan stood some trays up
("Rotate 90 on X, needs supports"), choosing the split approved those supports (#221 R1),
and the Ready question then offered "without supports" while those plates kept them.
"""
from print_handy.core import prepared_intake


def test_note_names_the_parts_a_split_turned_onto_supports():
    job = {"full_size_split_plan": {"reorientation_by_object": {
               "4": {"axis": "x", "supports": True}, "7": {"axis": "y", "supports": False}}},
           "color_manifest": {"parts": [{"object_id": "4", "name": "Tray Left.stl"},
                                        {"object_id": "7", "name": "Pad"}]}}
    note = prepared_intake.split_turn_supports_note(job)
    assert "Tray Left" in note and "Pad" not in note
    assert "keep the supports their new pose needs" in note


def test_no_note_without_a_supports_turn():
    assert prepared_intake.split_turn_supports_note({}) == ""
    assert prepared_intake.split_turn_supports_note(
        {"full_size_split_plan": {"reorientation_by_object": {"1": {"supports": False}}}}) == ""
