"""Paint states past 17 (TriangleSelector extended form).

BambuStudio writes a leaf state n >= 3 as 0xC, then (n - 3) // 15 nibbles of
0xF, then (n - 3) % 15. OrcaSlicer writes 18..32 as 0xC, 0xF, (n - 18): the
same rule with a run of one. Strings are stored reversed (read from the end).
Before this, a state of 18 or more refused with 'paint state too large', and
an F nibble after 0xC was misread as state 18.
"""
import pytest

from print_handy.core import painted_geometry as pg


def _leaf(state):
    return pg._encode(('l', state))


@pytest.mark.parametrize("state", [0, 1, 2, 3, 17, 18, 19, 25, 32, 33, 47, 48, 64])
def test_every_state_round_trips(state):
    assert pg.decode_states(_leaf(state)) == {state: 1}


def test_wire_form_matches_bambu_and_orca():
    # Reversed nibble strings, so the leading nibble is written last.
    assert _leaf(17) == "EC"          # 0xC, 14
    assert _leaf(18) == "0FC"         # 0xC, F, 0   (Orca: F then n-18 = 0)
    assert _leaf(25) == "7FC"         # 0xC, F, 7
    assert _leaf(33) == "0FFC"        # 0xC, F, F, 0 (Bambu run of two)


def test_orca_written_state_twenty_decodes_as_twenty():
    # Orca: "11" prefix (0xC), 0b1111, then n - 18 = 2. Reversed: "2FC".
    assert pg.decode_states("2FC") == {20: 1}


def test_remap_into_and_out_of_extended_states():
    split = pg._encode(('s', 3, [('l', 1), ('l', 20), ('l', 2), ('l', 40)]))
    remapped = pg.remap_code(split, {20: 2, 40: 1, 1: 22})
    assert pg.decode_states(remapped) == {22: 1, 2: 2, 1: 1}


def test_truncated_extended_state_is_refused():
    with pytest.raises(ValueError, match="truncated"):
        pg.decode_states("FC")
