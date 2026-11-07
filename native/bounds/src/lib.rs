//! Native G-code motion-bounds parser for Print Handy (#952, D1).
//!
//! This is a byte-for-byte Rust port of the semantics of
//! `print_handy.core.model_job._gcode_motion_bounds_python` (the Python
//! implementation, which remains the permanent fallback and test oracle).
//! The Python implementation is regex-driven; the semantic contract both
//! implementations share — and that the shared corpus in
//! `tests/pipeline/native_bounds_corpus.py` pins — is:
//!
//! * Lines are split on `\n`; only spaces and tabs may precede a command.
//! * Command recognition is line-anchored and case-insensitive, requires
//!   the next character to be whitespace or end-of-text, and tries the
//!   alternation in the Python pattern's order (`G00` before `G0`, ...),
//!   so `G1X10`, `G10`, `G92.1` and `G90;comment` do NOT match.
//! * The words after a command run to the first `;`, `\r` or `\n`.
//! * `G90`/`G91` switch absolute/relative positioning (no `M82`/`M83`).
//! * `G20` (inch units) refuses with its line number (API 2).
//! * `G28`, `G53`, `FORCE_MOVE`, `MANUAL_STEPPER`, `STEPPER_BUZZ` and
//!   `SET_KINEMATIC_POSITION` refuse with their line number; `G2`/`G02`/
//!   `G3`/`G03` arcs refuse; `G92` refuses only when its words contain an
//!   axis letter followed by a number.
//! * Axis extraction scans the words for `X`/`Y`/`Z` (either case)
//!   followed by optional whitespace and a number of the grammar
//!   `[+-]?(\d+(\.\d*)?|\.\d+)([Ee][+-]?\d+)?`; glued words (`X10Y20`)
//!   count, a number is read as its longest valid prefix (`X1.2.3` reads
//!   1.2), duplicate axis words apply in order, and bounds update per
//!   occurrence. Relative motion accumulates onto the current position
//!   and refuses on a NaN (never-positioned) axis.
//! * Float conversion is correctly rounded, exactly like Python's
//!   `float()` (Rust's `f64` parser is also correctly rounded; overflow
//!   yields infinity in both).
//!
//! Unicode scope: the Python patterns are Unicode-aware (`\d`, `\s`,
//! case folding). This port implements the ASCII subset of those
//! semantics, extended with the full Python whitespace set over UTF-8
//! for the command-boundary check. The Python dispatcher only ever
//! sends pure-ASCII text here (an ASCII gate in
//! `gcode_motion_bounds`), so at the public API level the two
//! implementations are equivalent by construction; non-ASCII input
//! always takes the Python path. No `unsafe` code.

use pyo3::create_exception;
use pyo3::prelude::*;
use pyo3::types::PyDict;

/// Version handshake between this extension and the Python dispatcher.
/// Bump on any semantic change; the dispatcher falls back to the Python
/// parser when the constant does not match its expectation.
pub const API_VERSION: u32 = 2; // 2: G20 refused (#952 phase 2)

create_exception!(
    _bounds_native,
    BoundsRefusal,
    pyo3::exceptions::PyRuntimeError,
    "A legitimate bounds-verifier refusal, carrying the same message the Python parser raises."
);

// ---------------------------------------------------------------------------
// Parse core (pure Rust; mirrors the Python semantics documented above)
// ---------------------------------------------------------------------------

/// Length in bytes of the Python-`\s` whitespace character starting at
/// `s[i]`, or 0. Covers the ASCII whitespace set plus the Unicode
/// whitespace characters encoded in UTF-8 (Python `\s` on `str`).
fn ws_len(s: &[u8], i: usize) -> usize {
    let n = s.len();
    let c = s[i];
    if matches!(c, 0x09 | 0x0A | 0x0B | 0x0C | 0x0D | 0x20 | 0x1C | 0x1D | 0x1E | 0x1F) {
        return 1;
    }
    if c == 0xC2 && i + 1 < n && (s[i + 1] == 0x85 || s[i + 1] == 0xA0) {
        return 2;
    }
    if i + 2 < n {
        if s[i] == 0xE1 && s[i + 1] == 0x9A && s[i + 2] == 0x80 {
            return 3;
        }
        if s[i] == 0xE2
            && s[i + 1] == 0x80
            && ((0x80..=0x8A).contains(&s[i + 2]) || s[i + 2] == 0xA8 || s[i + 2] == 0xA9 || s[i + 2] == 0xAF)
        {
            return 3;
        }
        if s[i] == 0xE2 && s[i + 1] == 0x81 && s[i + 2] == 0x9F {
            return 3;
        }
        if s[i] == 0xE3 && s[i + 1] == 0x80 && s[i + 2] == 0x80 {
            return 3;
        }
    }
    0
}

/// Correctly rounded float parse of a grammar-valid number token.
/// Rust's `f64` parser rejects two shapes Python's `float()` accepts —
/// a trailing dot (`"1."`) and a dot directly before the exponent
/// (`"1.e5"`) — so those are normalized to the equal-valued `"1.0"` /
/// `"1.0e5"` first. The numeric value is unchanged, so correct rounding
/// is preserved.
fn parse_f64_token(token: &[u8]) -> Option<f64> {
    let text = std::str::from_utf8(token).ok()?;
    if let Some(dot) = text.find('.') {
        let after = &text[dot + 1..];
        if after.is_empty() || after.starts_with(['e', 'E']) {
            let normalized = format!("{}0{}", &text[..=dot], after);
            return normalized.parse::<f64>().ok();
        }
    }
    text.parse::<f64>().ok()
}

/// Parse the NUMBER grammar in `s[j..b]` after optional whitespace.
/// Returns `(end, value)` where `end` is the index just past the longest
/// valid number prefix, or `None` when no number starts there.
fn parse_number(s: &[u8], b: usize, mut j: usize) -> Option<(usize, f64)> {
    while j < b {
        let w = ws_len(s, j);
        if w == 0 {
            break;
        }
        j += w;
    }
    let start = j;
    if j < b && (s[j] == b'+' || s[j] == b'-') {
        j += 1;
    }
    let digits_start = j;
    while j < b && s[j].is_ascii_digit() {
        j += 1;
    }
    let int_digits = j - digits_start;
    let mut end;
    if int_digits > 0 {
        end = j;
        if j < b && s[j] == b'.' {
            let mut k = j + 1;
            while k < b && s[k].is_ascii_digit() {
                k += 1;
            }
            end = k;
        }
    } else if j < b && s[j] == b'.' && j + 1 < b && s[j + 1].is_ascii_digit() {
        let mut k = j + 1;
        while k < b && s[k].is_ascii_digit() {
            k += 1;
        }
        end = k;
    } else {
        return None;
    }
    if end < b && (s[end] == b'e' || s[end] == b'E') {
        let mut k = end + 1;
        if k < b && (s[k] == b'+' || s[k] == b'-') {
            k += 1;
        }
        let es = k;
        while k < b && s[k].is_ascii_digit() {
            k += 1;
        }
        if k > es {
            end = k;
        }
    }
    parse_f64_token(&s[start..end]).map(|v| (end, v))
}

fn axis_index(c: u8) -> Option<usize> {
    match c {
        b'X' | b'x' => Some(0),
        b'Y' | b'y' => Some(1),
        b'Z' | b'z' => Some(2),
        _ => None,
    }
}

/// Commands in the Python pattern's alternation order.
const COMMANDS: &[&str] = &[
    "G90", "G91", "G00", "G0", "G01", "G1", "G02", "G2", "G03", "G3", "G92", "G28", "G53", "G20",
    "FORCE_MOVE", "MANUAL_STEPPER", "STEPPER_BUZZ", "SET_KINEMATIC_POSITION",
];

struct State {
    absolute: bool,
    pos: [f64; 3],
    low: [f64; 3],
    high: [f64; 3],
    recognized: u64,
}

impl State {
    /// Apply one axis word; `Err(axis)` when relative motion targets an
    /// axis with no absolute position yet (the NaN refusal).
    fn apply(&mut self, axis: usize, value: f64) -> Result<(), usize> {
        if self.absolute {
            self.pos[axis] = value;
        } else {
            if self.pos[axis].is_nan() {
                return Err(axis);
            }
            self.pos[axis] += value;
        }
        if self.pos[axis] < self.low[axis] {
            self.low[axis] = self.pos[axis];
        }
        if self.pos[axis] > self.high[axis] {
            self.high[axis] = self.pos[axis];
        }
        Ok(())
    }
}

/// Parse G-code text into `[[low, high]; 3]` for X, Y, Z, or return the
/// exact refusal message the Python implementation raises.
pub fn parse(s: &[u8]) -> Result<[[f64; 2]; 3], String> {
    let n = s.len();
    let mut st = State {
        absolute: true,
        pos: [f64::NAN; 3],
        low: [f64::INFINITY; 3],
        high: [f64::NEG_INFINITY; 3],
        recognized: 0,
    };
    let mut p = 0usize;
    let mut lineno: u64 = 1;
    loop {
        let mut line_end = p;
        while line_end < n && s[line_end] != b'\n' {
            line_end += 1;
        }
        let mut i = p;
        while i < line_end && (s[i] == b' ' || s[i] == b'\t') {
            i += 1;
        }
        let mut matched: Option<(&str, usize)> = None;
        for cand in COMMANDS {
            let len = cand.len();
            if i + len > n {
                continue;
            }
            let mut eq = true;
            for k in 0..len {
                let mut c = s[i + k];
                if c.is_ascii_lowercase() {
                    c = c.to_ascii_uppercase();
                }
                if c != cand.as_bytes()[k] {
                    eq = false;
                    break;
                }
            }
            if !eq {
                continue;
            }
            let j = i + len;
            if j == n || (j < n && ws_len(s, j) > 0) {
                matched = Some((cand, j));
                break;
            }
        }
        if let Some((cmd, words_start)) = matched {
            let mut words_end = words_start;
            while words_end < n && s[words_end] != b';' && s[words_end] != b'\r' && s[words_end] != b'\n' {
                words_end += 1;
            }
            match cmd {
                "G90" => st.absolute = true,
                "G91" => st.absolute = false,
                "G28" | "G53" | "FORCE_MOVE" | "MANUAL_STEPPER" | "STEPPER_BUZZ"
                | "SET_KINEMATIC_POSITION" => {
                    return Err(format!(
                        "line {lineno}: unsupported motion-affecting command {cmd}"
                    ));
                }
                "G2" | "G02" | "G3" | "G03" => {
                    return Err(format!(
                        "line {lineno}: arc motion is unsupported by the bounds verifier"
                    ));
                }
                "G20" => {
                    // Inch coordinates read as millimetres would shrink every
                    // bound about 25x and pass a print that cannot fit.
                    return Err(format!("line {lineno}: inch units (G20) are unsupported by the bounds verifier"));
                }
                "G92" => {
                    let mut found = false;
                    let mut k = words_start;
                    while k < words_end && !found {
                        if axis_index(s[k]).is_some()
                            && parse_number(s, words_end, k + 1).is_some()
                        {
                            found = true;
                        }
                        k += 1;
                    }
                    if found {
                        return Err(format!(
                            "line {lineno}: G92 changes XYZ coordinates; bounds are ambiguous"
                        ));
                    }
                }
                _ => {
                    // G0/G00/G1/G01: scan the words for axis+number pairs.
                    let mut found = false;
                    let mut k = words_start;
                    while k < words_end {
                        if let Some(ax) = axis_index(s[k]) {
                            if let Some((end, v)) = parse_number(s, words_end, k + 1) {
                                found = true;
                                if let Err(axis) = st.apply(ax, v) {
                                    let name = b"XYZ"[axis] as char;
                                    return Err(format!(
                                        "line {lineno}: relative {name} motion before an absolute {name} position"
                                    ));
                                }
                                k = end;
                                continue;
                            }
                        }
                        k += 1;
                    }
                    if found {
                        st.recognized += 1;
                    }
                }
            }
        }
        if line_end >= n {
            break;
        }
        p = line_end + 1;
        lineno += 1;
        if p > n {
            break;
        }
    }
    if st.recognized == 0 {
        return Err("no bounded XYZ motion found".to_string());
    }
    let names = ["X", "Y", "Z"];
    let missing: Vec<&str> = (0..3)
        .filter(|&a| st.low[a] == f64::INFINITY)
        .map(|a| names[a])
        .collect();
    if !missing.is_empty() {
        return Err(format!("missing bounded motion for axis {}", missing.join(", ")));
    }
    Ok([
        [st.low[0], st.high[0]],
        [st.low[1], st.high[1]],
        [st.low[2], st.high[2]],
    ])
}

// ---------------------------------------------------------------------------
// PyO3 wrapper
// ---------------------------------------------------------------------------

/// Parse G-code text into `{"x": (low, high), "y": ..., "z": ...}`,
/// raising `BoundsRefusal` (a `RuntimeError`) with the Python parser's
/// exact message on a refusal.
#[pyfunction]
fn parse_bounds(py: Python<'_>, text: &str) -> PyResult<Py<PyDict>> {
    // The parse touches no Python object: release the GIL so the bot's
    // other threads keep running while a large plate is verified.
    let bytes = text.as_bytes();
    let outcome = py.detach(|| parse(bytes));
    match outcome {
        Ok(b) => {
            let dict = PyDict::new(py);
            dict.set_item("x", (b[0][0], b[0][1]))?;
            dict.set_item("y", (b[1][0], b[1][1]))?;
            dict.set_item("z", (b[2][0], b[2][1]))?;
            Ok(dict.unbind())
        }
        Err(msg) => Err(BoundsRefusal::new_err(msg)),
    }
}

#[pymodule]
fn _bounds_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("API_VERSION", API_VERSION)?;
    m.add_function(wrap_pyfunction!(parse_bounds, m)?)?;
    m.add("BoundsRefusal", m.py().get_type::<BoundsRefusal>())?;
    Ok(())
}
