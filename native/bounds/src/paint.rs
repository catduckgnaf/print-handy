//! Native port of the painted 3MF paint census (#1715).
//!
//! The Python oracle is `print_handy.core.painted_geometry.leaf_states`,
//! which stays forever as the fallback. Its contract, per `.model`
//! member: for one mesh object, the paint-state leaf counts over its
//! triangles plus the unpainted triangle count. This port computes that
//! census for every object in the member in one pass; the Python
//! dispatcher serves each `leaf_states` call from it and keeps the
//! Python counting for parts with `triangle_ranges` (a Prusa volume
//! split counts a subset of a mesh's triangles by index, which a
//! whole-mesh census cannot answer — that fallback is the design, not
//! an accident).
//!
//! The semantics mirrored from the Python implementation:
//!
//! * Object blocks are the matches of
//!   `<(?:\w+:)?object\b[^>]*\bid="(\d+)".*?</(?:\w+:)?object>` (dotall)
//!   over the member: an optional namespace prefix, a word boundary
//!   after `object`, and an all-digit `id` attribute inside the opening
//!   tag (before its first `>`). Because `[^>]*` is greedy, the LAST
//!   valid `id="..."` in the tag wins; the char before `id` must not be
//!   a word char (so `objectid=` never matches but `data-id=` does).
//!   A block's span runs to the FIRST closing tag, whatever prefix it
//!   carries, and scanning resumes after it — an unclosed outer object
//!   swallows an inner object's close exactly as the regex does. A
//!   repeated id keeps the last span, like the dict comprehension that
//!   builds the Python map.
//! * Inside a block's span, triangle tags are the matches of
//!   `<(?:\w+:)?triangle\b[^>]*>` over the span slice. A tag's paint
//!   code is the first `paint_color="HEX"` whose value is non-empty
//!   hex closed by a quote, else the first such
//!   `mmu_segmentation="HEX"` (the `slic3rpe:` prefix is optional and
//!   unanchored, so the bare substring is what is searched for).
//!   paint_color wins when both are present. The attribute name is a
//!   plain substring search, so `xpaint_color="..."` matches too.
//! * Codes decode with the TriangleSelector tree: hex nibbles read
//!   from the END of the string; per node the low 2 bits are the split
//!   count (0 = leaf, else split+1 children follow); a leaf's state is
//!   the high 2 bits, with state 3 extended by a run of 0xF nibbles
//!   (15 each) and one final nibble. Decode failures carry Python's
//!   exact messages (`truncated paint_color`, `trailing paint_color
//!   data`) and are reported per object, so a bad mesh never poisons
//!   the census of the member's other meshes — the Python caller
//!   raises the same ValueError only when that mesh is actually asked
//!   for, exactly as the Python decode does.
//!
//! One known divergence class: Python's `_parse` is recursive, so a
//! paint code nested deeper than the interpreter's recursion limit
//! raises RecursionError there; this iterative walk counts it. Slicer
//! paint codes nest a handful of levels; nothing else differs.

use std::collections::BTreeMap;

/// Version handshake with the Python dispatcher. Bump on any semantic change.
pub const PAINT_CENSUS_VERSION: u32 = 1;

/// One mesh object's census row: the state counts, or the decode error
/// the Python implementation raises for that mesh (its exact message).
pub enum CensusRow {
    Counts { states: BTreeMap<u64, u64>, unpainted: u64 },
    Error(&'static str),
}

fn is_word(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_'
}

fn find(hay: &[u8], needle: &[u8], from: usize) -> Option<usize> {
    if from > hay.len() || needle.is_empty() || hay.len() - from < needle.len() {
        return None;
    }
    hay[from..]
        .windows(needle.len())
        .position(|w| w == needle)
        .map(|p| p + from)
}

/// Match a tag name at `i` (the byte just after `<` or `</`): an
/// optional `\w+:` namespace prefix, then `name`, then a word boundary
/// (the next byte is not a word char, or the input ends). Returns the
/// index just past the name.
fn tag_name_end(data: &[u8], i: usize, name: &[u8]) -> Option<usize> {
    // Prefixed form: a maximal run of word chars, a colon, then the name.
    let mut j = i;
    while j < data.len() && is_word(data[j]) {
        j += 1;
    }
    if j > i && j < data.len() && data[j] == b':' {
        if let Some(end) = name_at(data, j + 1, name) {
            return Some(end);
        }
    }
    name_at(data, i, name)
}

fn name_at(data: &[u8], i: usize, name: &[u8]) -> Option<usize> {
    if data.len() - i.min(data.len()) < name.len() || &data[i..i + name.len()] != name {
        return None;
    }
    let end = i + name.len();
    if end < data.len() && is_word(data[end]) {
        return None;
    }
    Some(end)
}

/// The object id of an opening tag: the LAST `id="(\d+)"` in the tag
/// whose `id` is preceded by a non-word char and whose value is all
/// digits closed by a quote (the greedy `[^>]*` in the Python pattern
/// backtracks from the right, so the rightmost valid attribute wins).
fn object_id(tag: &[u8]) -> Option<String> {
    let mut found: Option<String> = None;
    let mut from = 0;
    while let Some(p) = find(tag, b"id=\"", from) {
        from = p + 1;
        if p > 0 && is_word(tag[p - 1]) {
            continue;
        }
        let start = p + 4;
        let mut e = start;
        while e < tag.len() && tag[e].is_ascii_digit() {
            e += 1;
        }
        if e > start && e < tag.len() && tag[e] == b'"' {
            found = Some(String::from_utf8_lossy(&tag[start..e]).into_owned());
        }
    }
    found
}

/// The end (one past `>`) of the first `</(?:\w+:)?object>` at or after
/// `from`, or None when the member has no closing object tag there.
fn object_close_end(data: &[u8], from: usize) -> Option<usize> {
    let mut pos = from;
    while let Some(p) = find(data, b"</", pos) {
        pos = p + 1;
        if let Some(end) = tag_name_end(data, p + 2, b"object") {
            if end < data.len() && data[end] == b'>' {
                return Some(end + 1);
            }
        }
    }
    None
}

/// (id, span start, span end) of every object block, in document order:
/// the finditer matches of the Python `_object_blocks` pattern.
fn object_blocks(data: &[u8]) -> Vec<(String, usize, usize)> {
    let mut blocks = Vec::new();
    let mut pos = 0;
    while let Some(lt) = find(data, b"<", pos) {
        pos = lt + 1;
        let Some(name_end) = tag_name_end(data, lt + 1, b"object") else {
            continue;
        };
        let Some(gt) = find(data, b">", name_end) else {
            break; // no '>' left: no opening tag can complete anywhere
        };
        let Some(id) = object_id(&data[lt..gt]) else {
            continue; // finditer advances one char and retries
        };
        let Some(close_end) = object_close_end(data, gt + 1) else {
            break; // no closing tag left: no later object can match either
        };
        blocks.push((id, lt, close_end));
        pos = close_end;
    }
    blocks
}

/// The first `needle` occurrence in `tag` whose value is non-empty hex
/// closed by a quote — the match of `needle([0-9A-Fa-f]+)"` searched
/// left to right, exactly as the Python regex search behaves.
fn attr_code<'a>(tag: &'a [u8], needle: &[u8]) -> Option<&'a [u8]> {
    let mut from = 0;
    while let Some(p) = find(tag, needle, from) {
        from = p + 1;
        let start = p + needle.len();
        let mut e = start;
        while e < tag.len() && tag[e].is_ascii_hexdigit() {
            e += 1;
        }
        if e > start && e < tag.len() && tag[e] == b'"' {
            return Some(&tag[start..e]);
        }
    }
    None
}

/// A triangle tag's paint code: paint_color wins over mmu_segmentation.
fn paint_code(tag: &[u8]) -> Option<&[u8]> {
    attr_code(tag, b"paint_color=\"")
        .or_else(|| attr_code(tag, b"mmu_segmentation=\""))
}

fn hex_val(b: u8) -> u32 {
    // Callers only pass regex-validated hex; anything else maps to 0
    // and cannot be reached.
    match b {
        b'0'..=b'9' => (b - b'0') as u32,
        b'a'..=b'f' => (b - b'a' + 10) as u32,
        b'A'..=b'F' => (b - b'A' + 10) as u32,
        _ => 0,
    }
}

/// Decode one paint code into state -> leaf count, with Python's exact
/// error messages. Iterative form of the recursive `_parse` + `_states`.
fn decode_states(code: &[u8]) -> Result<BTreeMap<u64, u64>, &'static str> {
    // Nibbles in parse order = the code's hex chars read from the end.
    let nib: Vec<u32> = code.iter().rev().map(|&b| hex_val(b)).collect();
    let mut pos = 0usize;
    let mut counts: BTreeMap<u64, u64> = BTreeMap::new();
    // Children still to visit for the open split nodes.
    let mut stack: Vec<u32> = Vec::new();
    loop {
        if pos >= nib.len() {
            return Err("truncated paint_color");
        }
        let c = nib[pos];
        pos += 1;
        let split = c & 3;
        if split > 0 {
            stack.push(split + 1);
        } else {
            let mut state = (c >> 2) as u64;
            if state == 3 {
                let mut run: u64 = 0;
                loop {
                    if pos >= nib.len() {
                        return Err("truncated paint_color");
                    }
                    let n = nib[pos];
                    pos += 1;
                    if n != 0xF {
                        state = 3 + 15 * run + n as u64;
                        break;
                    }
                    run += 1;
                }
            }
            *counts.entry(state).or_insert(0) += 1;
            // Close the split nodes this leaf completed.
            loop {
                match stack.last_mut() {
                    None => break,
                    Some(top) => {
                        *top -= 1;
                        if *top == 0 {
                            stack.pop();
                        } else {
                            break;
                        }
                    }
                }
            }
            if stack.is_empty() {
                break;
            }
        }
    }
    if pos != nib.len() {
        return Err("trailing paint_color data");
    }
    Ok(counts)
}

/// Count one object block's span (the slice the Python code counts).
fn count_span(body: &[u8]) -> CensusRow {
    let mut states: BTreeMap<u64, u64> = BTreeMap::new();
    let mut unpainted: u64 = 0;
    let mut pos = 0usize;
    while let Some(lt) = find(body, b"<", pos) {
        pos = lt + 1;
        let Some(name_end) = tag_name_end(body, lt + 1, b"triangle") else {
            continue;
        };
        let Some(gt) = find(body, b">", name_end) else {
            break; // the tag cannot complete inside the slice
        };
        let tag = &body[lt..=gt];
        match paint_code(tag) {
            Some(code) => match decode_states(code) {
                Ok(counts) => {
                    for (s, n) in counts {
                        *states.entry(s).or_insert(0) += n;
                    }
                }
                Err(msg) => return CensusRow::Error(msg),
            },
            None => unpainted += 1,
        }
        pos = gt + 1;
    }
    CensusRow::Counts { states, unpainted }
}

/// The census of one `.model` member: `{object_id: row}` for every
/// object block, a repeated id keeping its last span.
pub fn paint_census(model: &[u8]) -> BTreeMap<String, CensusRow> {
    let mut spans: BTreeMap<String, (usize, usize)> = BTreeMap::new();
    for (id, start, end) in object_blocks(model) {
        spans.insert(id, (start, end));
    }
    let mut out = BTreeMap::new();
    for (id, (start, end)) in spans {
        out.insert(id, count_span(&model[start..end]));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn counts(row: &CensusRow) -> (BTreeMap<u64, u64>, u64) {
        match row {
            CensusRow::Counts { states, unpainted } => (states.clone(), *unpainted),
            CensusRow::Error(msg) => panic!("unexpected error row: {msg}"),
        }
    }

    #[test]
    fn plain_and_prefixed_objects_census_their_triangles() {
        let model = b"<model><resources>\
            <object id=\"1\"><mesh><triangles>\
            <triangle paint_color=\"4\"/><triangle paint_color=\"8\"/><triangle/>\
            </triangles></mesh></object>\
            <p:object id=\"2\"><mesh><triangles>\
            <p:triangle slic3rpe:mmu_segmentation=\"8\"/>\
            </triangles></mesh></p:object>\
            </resources></model>";
        let census = paint_census(model);
        let (states, unpainted) = counts(&census["1"]);
        assert_eq!(states, BTreeMap::from([(1, 1), (2, 1)]));
        assert_eq!(unpainted, 1);
        let (states, unpainted) = counts(&census["2"]);
        assert_eq!(states, BTreeMap::from([(2, 1)]));
        assert_eq!(unpainted, 0);
    }

    #[test]
    fn extended_and_split_codes_decode_like_python() {
        let model = b"<object id=\"7\"><mesh><triangles>\
            <triangle paint_color=\"841\"/><triangle paint_color=\"0FC\"/>\
            </triangles></mesh></object>";
        let census = paint_census(model);
        let (states, _) = counts(&census["7"]);
        assert_eq!(states, BTreeMap::from([(1, 1), (2, 1), (18, 1)]));
    }

    #[test]
    fn a_decode_error_is_reported_per_object_with_pythons_message() {
        let model = b"<object id=\"1\"><mesh><triangles>\
            <triangle paint_color=\"1\"/></triangles></mesh></object>\
            <object id=\"2\"><mesh><triangles>\
            <triangle paint_color=\"44\"/></triangles></mesh></object>";
        let census = paint_census(model);
        assert!(matches!(census["1"], CensusRow::Error("truncated paint_color")));
        assert!(matches!(census["2"], CensusRow::Error("trailing paint_color data")));
    }

    #[test]
    fn the_last_id_attribute_and_the_last_duplicate_span_win() {
        let model = b"<object id=\"5\" id=\"7\"><mesh><triangles>\
            <triangle paint_color=\"4\"/></triangles></mesh></object>\
            <object id=\"7\"><mesh><triangles><triangle/></triangles></mesh></object>";
        let census = paint_census(model);
        assert_eq!(census.len(), 1);
        let (states, unpainted) = counts(&census["7"]);
        assert!(states.is_empty());
        assert_eq!(unpainted, 1);
    }
}
