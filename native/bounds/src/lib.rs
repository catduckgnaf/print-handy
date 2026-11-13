//! Native G-code motion-bounds parser for Print Handy (#952, D1).
//!
//! This is a byte-for-byte Rust port of the semantics of
//! `print_handy.core.model_job._gcode_motion_bounds_python` (the Python
//! implementation, which remains the permanent fallback and test oracle).
//! The Python implementation is regex-driven; the semantic contract both
//! implementations share — and that the shared corpus in
//! `tests/pipeline/native_bounds_corpus.py` pins — is:
//!
//! * A carriage return followed by anything but whitespace before the
//!   next `\n` refuses first, with its line number (API 3): Marlin ends
//!   the line there and Klipper does not.
//! * Lines are split on `\n`. A line is tokenized the way Klipper and
//!   Marlin read it (API 3): optional leading whitespace (Python `\s`
//!   except `\n`), an optional `N<digits>` line number and whitespace,
//!   then either a `G`/`M` command word (letter, optional whitespace,
//!   ASCII digits with leading zeros ignored, optional `.<digits>`
//!   subcode; parameters may be glued on: `G1X400`) or an extended
//!   command name `[A-Za-z_][A-Za-z0-9_]*`. Case-insensitive.
//! * The words after a command run to the first `;` or `\n`.
//! * `G90`/`G91` switch absolute/relative positioning (no `M82`/`M83`).
//! * `G20` (inch units) refuses with its line number (API 2).
//! * `G28`, `G53`, `G5`, `G6`, `G38`, `FORCE_MOVE`, `MANUAL_STEPPER`,
//!   `STEPPER_BUZZ` and `SET_KINEMATIC_POSITION` refuse with their line
//!   number; `G2`/`G3` arcs refuse; `G92` refuses only when its words
//!   contain an axis letter followed by a number.
//! * Offset commands refuse (API 3): `SET_GCODE_OFFSET`,
//!   `RESTORE_GCODE_STATE`, `M428`, `G54`..`G59`, `M206` with an axis
//!   word, and `G10` with an `L` or axis word. A subcode on any G number
//!   the parser interprets (`G92.1`, `G91.1`, `G1.5`) refuses; `M`
//!   commands are matched on their number whatever the subcode.
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
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

mod bed_scan;

/// Version handshake between this extension and the Python dispatcher.
/// Bump on any semantic change; the dispatcher falls back to the Python
/// parser when the constant does not match its expectation.
// 2: G20 refused (#952 phase 2). 3: firmware tokenization and offset
// commands refused (S4).
pub const API_VERSION: u32 = 3;

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

/// Extended commands that refuse as unsupported motion.
const FORBIDDEN_NAMES: &[&str] = &["FORCE_MOVE", "MANUAL_STEPPER", "STEPPER_BUZZ", "SET_KINEMATIC_POSITION"];
/// Extended commands that change what later coordinates mean.
const OFFSET_NAMES: &[&str] = &["SET_GCODE_OFFSET", "RESTORE_GCODE_STATE"];
/// G numbers (leading zeros stripped) that refuse as unsupported motion.
const G_FORBIDDEN: &[&[u8]] = &[b"28", b"53", b"5", b"6", b"38"];
/// G numbers that select a work coordinate system.
const G_WORK_OFFSETS: &[&[u8]] = &[b"54", b"55", b"56", b"57", b"58", b"59"];
/// G numbers the parser interprets; a subcode on any of them refuses.
const G_KNOWN: &[&[u8]] = &[
    b"0", b"1", b"2", b"3", b"5", b"6", b"10", b"20", b"28", b"38", b"53", b"90", b"91", b"92",
    b"54", b"55", b"56", b"57", b"58", b"59",
];

/// One recognized line-start command.
enum Command<'a> {
    /// A `G`/`M` word: uppercase letter, number without leading zeros,
    /// optional subcode digits.
    Word { letter: u8, number: &'a [u8], sub: Option<&'a [u8]> },
    /// An extended command name, uppercased.
    Name(String),
}

/// Skip Python-`\s` whitespace in `s[i..end]` (never crosses `end`).
fn skip_ws(s: &[u8], mut i: usize, end: usize) -> usize {
    while i < end {
        let w = ws_len(s, i);
        if w == 0 || s[i] == b'\n' {
            break;
        }
        i += w;
    }
    i
}

fn is_name_char(c: u8) -> bool {
    c.is_ascii_alphanumeric() || c == b'_'
}

/// The command starting at `s[i]` (within `s[..end]`) and the index its
/// words start at, or `None` when no command starts there. Mirrors the
/// Python pattern `([GM])[^\S\n]*([0-9]+)(?:\.([0-9]+))?|([A-Z_][A-Z0-9_]*)`.
fn parse_command(s: &[u8], i: usize, end: usize) -> Option<(Command<'_>, usize)> {
    if i >= end {
        return None;
    }
    let c = s[i].to_ascii_uppercase();
    if c == b'G' || c == b'M' {
        let ds = skip_ws(s, i + 1, end);
        let mut j = ds;
        while j < end && s[j].is_ascii_digit() {
            j += 1;
        }
        if j > ds {
            let mut nz = ds;
            while nz + 1 < j && s[nz] == b'0' {
                nz += 1;
            }
            let number = &s[nz..j];
            let mut sub = None;
            if j + 1 < end && s[j] == b'.' && s[j + 1].is_ascii_digit() {
                let mut k = j + 1;
                while k < end && s[k].is_ascii_digit() {
                    k += 1;
                }
                sub = Some(&s[j + 1..k]);
                j = k;
            }
            return Some((Command::Word { letter: c, number, sub }, j));
        }
    }
    if c.is_ascii_alphabetic() || c == b'_' {
        let mut k = i + 1;
        while k < end && is_name_char(s[k]) {
            k += 1;
        }
        let name = String::from_utf8_lossy(&s[i..k]).to_ascii_uppercase();
        return Some((Command::Name(name), k));
    }
    None
}

/// True when `s[start..end]` holds one of `letters` (either case)
/// followed by optional whitespace and a number.
fn has_word(s: &[u8], start: usize, end: usize, letters: &[u8]) -> bool {
    let mut k = start;
    while k < end {
        if letters.contains(&s[k].to_ascii_uppercase()) && parse_number(s, end, k + 1).is_some() {
            return true;
        }
        k += 1;
    }
    false
}

fn digits(b: &[u8]) -> String {
    String::from_utf8_lossy(b).into_owned()
}

fn is(number: &[u8], want: &[u8]) -> bool {
    number == want
}

fn one_of(set: &[&[u8]], number: &[u8]) -> bool {
    set.iter().any(|known| *known == number)
}

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
    // A carriage return with more text after it on the same line: Marlin
    // ends the line at the CR, Klipper does not, so they run different
    // commands. Refused before anything else, like the Python pre-pass.
    {
        let mut cr_line: u64 = 1;
        for i in 0..n {
            if s[i] == b'\n' {
                cr_line += 1;
            } else if s[i] == b'\r' {
                let mut j = i + 1;
                loop {
                    if j >= n || s[j] == b'\n' {
                        break;
                    }
                    let w = ws_len(s, j);
                    if w == 0 {
                        return Err(format!(
                            "line {cr_line}: carriage return inside a line; firmwares split it \
                             differently, so bounds are ambiguous"
                        ));
                    }
                    j += w;
                }
            }
        }
    }
    let mut p = 0usize;
    let mut lineno: u64 = 1;
    loop {
        let mut line_end = p;
        while line_end < n && s[line_end] != b'\n' {
            line_end += 1;
        }
        let i = skip_ws(s, p, line_end);
        let parsed = if i + 1 < line_end && (s[i] == b'N' || s[i] == b'n') && s[i + 1].is_ascii_digit() {
            // A line number. When no command follows it, the Python pattern
            // falls back to reading the line as a name starting with N,
            // which no refusal set contains: the line is ignored either way.
            let mut j = i + 1;
            while j < line_end && s[j].is_ascii_digit() {
                j += 1;
            }
            parse_command(s, skip_ws(s, j, line_end), line_end)
        } else {
            parse_command(s, i, line_end)
        };
        if let Some((cmd, words_start)) = parsed {
            let mut words_end = words_start;
            while words_end < line_end && s[words_end] != b';' {
                words_end += 1;
            }
            match cmd {
                Command::Name(name) => {
                    if FORBIDDEN_NAMES.iter().any(|known| *known == name) {
                        return Err(format!("line {lineno}: unsupported motion-affecting command {name}"));
                    }
                    if OFFSET_NAMES.iter().any(|known| *known == name) {
                        return Err(format!(
                            "line {lineno}: {name} changes coordinate offsets; bounds are ambiguous"
                        ));
                    }
                }
                Command::Word { letter: b'M', number, .. } => {
                    // Marlin dispatches on the number alone, whatever the subcode.
                    if is(number, b"428") || (is(number, b"206") && has_word(s, words_start, words_end, b"XYZ")) {
                        return Err(format!(
                            "line {lineno}: M{} changes coordinate offsets; bounds are ambiguous",
                            digits(number)
                        ));
                    }
                }
                Command::Word { number, sub: Some(sub), .. } => {
                    if one_of(G_KNOWN, number) {
                        return Err(format!(
                            "line {lineno}: unsupported motion-affecting command G{}.{}",
                            digits(number),
                            digits(sub)
                        ));
                    }
                }
                Command::Word { number, sub: None, .. } => {
                    if is(number, b"0") || is(number, b"1") {
                        // G0/G1: scan the words for axis+number pairs.
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
                    } else if is(number, b"90") {
                        st.absolute = true;
                    } else if is(number, b"91") {
                        st.absolute = false;
                    } else if is(number, b"92") {
                        if has_word(s, words_start, words_end, b"XYZ") {
                            return Err(format!(
                                "line {lineno}: G92 changes XYZ coordinates; bounds are ambiguous"
                            ));
                        }
                    } else if is(number, b"2") || is(number, b"3") {
                        return Err(format!(
                            "line {lineno}: arc motion is unsupported by the bounds verifier"
                        ));
                    } else if is(number, b"20") {
                        // Inch coordinates read as millimetres would shrink every
                        // bound about 25x and pass a print that cannot fit.
                        return Err(format!("line {lineno}: inch units (G20) are unsupported by the bounds verifier"));
                    } else if one_of(G_FORBIDDEN, number) {
                        return Err(format!(
                            "line {lineno}: unsupported motion-affecting command G{}",
                            digits(number)
                        ));
                    } else if one_of(G_WORK_OFFSETS, number)
                        || (is(number, b"10") && has_word(s, words_start, words_end, b"LXYZ"))
                    {
                        return Err(format!(
                            "line {lineno}: G{} changes coordinate offsets; bounds are ambiguous",
                            digits(number)
                        ));
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

/// The parse behind a `catch_unwind` boundary (#1017, defense in depth).
///
/// The parser is written to refuse, never to panic, but a panic that did
/// escape would cross the FFI boundary as PyO3's `PanicException` — a
/// `BaseException` the Python dispatcher already falls back on, at the
/// cost of an ugly unwind through foreign frames. Catching it here turns
/// it into an ordinary refusal with a named message, so the dispatcher's
/// fallback sees the same shape as any other refusal. This is not a
/// `panic = "abort"` change: unwinding stays on, the panic is caught.
fn guarded_parse(bytes: &[u8]) -> Result<[[f64; 2]; 3], String> {
    unwind_guard(|| parse(bytes))
}

/// Run `f` behind the unwind boundary: a panic becomes the named refusal
/// instead of propagating. Split out so the boundary itself is testable.
fn unwind_guard<T>(f: impl FnOnce() -> Result<T, String>) -> Result<T, String> {
    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(f)) {
        Ok(outcome) => outcome,
        Err(_) => Err("native bounds parser panicked; refusing the plate".to_string()),
    }
}

/// Parse G-code text into `{"x": (low, high), "y": ..., "z": ...}`,
/// raising `BoundsRefusal` (a `RuntimeError`) with the Python parser's
/// exact message on a refusal.
#[pyfunction]
fn parse_bounds(py: Python<'_>, text: &str) -> PyResult<Py<PyDict>> {
    // The parse touches no Python object: release the GIL so the bot's
    // other threads keep running while a large plate is verified.
    let bytes = text.as_bytes();
    let outcome = py.detach(|| guarded_parse(bytes));
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

/// The first `(x, y)` a G0-G3 move puts off a `bed_x` by `bed_y` mm bed,
/// or None (#1392; the contract is in `bed_scan.rs`). Any internal error
/// raises `ValueError`, on which the Python dispatcher falls back.
#[pyfunction]
fn first_off_bed(py: Python<'_>, text: &str, bed_x: f64, bed_y: f64) -> PyResult<Option<(f64, f64)>> {
    let bytes = text.as_bytes();
    let outcome = py.detach(|| unwind_guard(|| bed_scan::first_off_bed(bytes, bed_x, bed_y)));
    outcome.map_err(PyValueError::new_err)
}

#[pymodule]
fn _bounds_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("API_VERSION", API_VERSION)?;
    m.add("BED_SCAN_VERSION", bed_scan::BED_SCAN_VERSION)?;
    m.add_function(wrap_pyfunction!(parse_bounds, m)?)?;
    m.add_function(wrap_pyfunction!(first_off_bed, m)?)?;
    m.add("BoundsRefusal", m.py().get_type::<BoundsRefusal>())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn guarded_parse_reads_simple_bounds() {
        let outcome = guarded_parse(b"G90\nG1 X10 Y10 Z1\nG1 X20 Y5 Z2\n");
        assert_eq!(outcome, Ok([[10.0, 20.0], [5.0, 10.0], [1.0, 2.0]]));
    }

    #[test]
    fn guarded_parse_keeps_an_ordinary_refusal() {
        let outcome = guarded_parse(b"G20\nG1 X10 Y10 Z1\n");
        assert!(outcome.unwrap_err().contains("G20"), "refusals pass through unchanged");
    }

    #[test]
    fn firmware_tokenization_sees_glued_and_commented_commands() {
        let outcome = guarded_parse(b"G90\nG1 X10 Y1 Z1\nG91;rel\nG1 X100\nN7 G1X100*12\n");
        assert_eq!(outcome, Ok([[10.0, 210.0], [1.0, 1.0], [1.0, 1.0]]));
    }

    #[test]
    fn offset_commands_refuse_by_name() {
        let outcome = guarded_parse(b"G90\nG1 X1 Y1 Z1\nSET_GCODE_OFFSET X=300\n");
        assert_eq!(outcome.unwrap_err(),
                   "line 3: SET_GCODE_OFFSET changes coordinate offsets; bounds are ambiguous");
        let outcome = guarded_parse(b"G90\nG1 X1 Y1 Z1\nM206 X300\n");
        assert!(outcome.unwrap_err().contains("M206"));
    }

    #[test]
    fn unwind_guard_turns_a_panic_into_the_named_refusal() {
        let outcome: Result<(), String> = unwind_guard(|| panic!("simulated parser panic"));
        assert_eq!(outcome.unwrap_err(),
                   "native bounds parser panicked; refusing the plate");
    }
}
