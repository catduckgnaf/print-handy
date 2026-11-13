//! Native port of the post-slice bed check (#1392).
//!
//! The Python oracle is `print_handy.gcode.bed_scan._first_off_bed_python`,
//! which stays forever as the fallback. Its contract, for pure-ASCII text
//! (the dispatcher never sends anything else here):
//!
//! * Lines are `str.splitlines()` lines: split on `\n`, `\r`, `\x0b`,
//!   `\x0c`, `\x1c`, `\x1d` and `\x1e` (`\r\n` is one break; splitting it
//!   as two only adds an empty line, which never matches).
//! * A move line matches `^\s*G[0-3]\b([^;]*)` case-insensitively: Python
//!   `\s` inside a line is space, tab or `\x1f`; `\b` after the digit
//!   needs the end of the line or a non-word character (word is
//!   `[A-Za-z0-9_]`). The words run to the first `;`.
//! * In the words, `\b([XY])\s*(-?(?:\d+(?:\.\d*)?|\.\d+))` is found left
//!   to right without overlap, case-insensitively: the axis letter must
//!   start the words or follow a non-word character, and scanning resumes
//!   after each match. Each match sets that axis (last one wins).
//! * X and Y start at 0. The first move line that set an axis while X or
//!   Y is below 0 or above the bed size is the result, as (x, y).
//!
//! Float conversion is correctly rounded in both languages.

/// Version handshake with the Python dispatcher. Bump on any semantic change.
pub const BED_SCAN_VERSION: u32 = 1;

fn is_line_break(b: u8) -> bool {
    matches!(b, b'\n' | b'\r' | 0x0b | 0x0c | 0x1c | 0x1d | 0x1e)
}

fn is_inline_space(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | 0x1f)
}

fn is_word(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_'
}

/// The words of a move line (after `G0`..`G3`, up to the first `;`), or None.
fn move_words(line: &[u8]) -> Option<&[u8]> {
    let mut i = 0;
    while i < line.len() && is_inline_space(line[i]) {
        i += 1;
    }
    if i + 1 >= line.len() || !matches!(line[i], b'G' | b'g') || !(b'0'..=b'3').contains(&line[i + 1]) {
        return None;
    }
    let rest = &line[i + 2..];
    if let Some(&next) = rest.first() {
        if is_word(next) {
            return None;
        }
    }
    let end = rest.iter().position(|&b| b == b';').unwrap_or(rest.len());
    Some(&rest[..end])
}

/// Length of `-?(?:\d+(?:\.\d*)?|\.\d+)` at the start of `s`, or 0.
fn number_len(s: &[u8]) -> usize {
    let mut i = 0;
    if s.first() == Some(&b'-') {
        i = 1;
    }
    let digits_start = i;
    while i < s.len() && s[i].is_ascii_digit() {
        i += 1;
    }
    if i > digits_start {
        if i < s.len() && s[i] == b'.' {
            i += 1;
            while i < s.len() && s[i].is_ascii_digit() {
                i += 1;
            }
        }
        return i;
    }
    if i < s.len() && s[i] == b'.' && i + 1 < s.len() && s[i + 1].is_ascii_digit() {
        i += 1;
        while i < s.len() && s[i].is_ascii_digit() {
            i += 1;
        }
        return i;
    }
    0
}

/// Apply the X/Y words of one move line; true when any axis word matched.
fn apply_words(words: &[u8], x: &mut f64, y: &mut f64) -> Result<bool, String> {
    let mut moved = false;
    let mut i = 0;
    while i < words.len() {
        let axis = words[i];
        let boundary = i == 0 || !is_word(words[i - 1]);
        if boundary && matches!(axis, b'X' | b'x' | b'Y' | b'y') {
            let mut j = i + 1;
            while j < words.len() && is_inline_space(words[j]) {
                j += 1;
            }
            let len = number_len(&words[j..]);
            if len > 0 {
                let text = std::str::from_utf8(&words[j..j + len]).map_err(|e| e.to_string())?;
                let value: f64 = text.parse().map_err(|e| format!("bad number {text:?}: {e}"))?;
                if matches!(axis, b'X' | b'x') {
                    *x = value;
                } else {
                    *y = value;
                }
                moved = true;
                i = j + len;
                continue;
            }
        }
        i += 1;
    }
    Ok(moved)
}

/// The first (x, y) a move puts off a `bed_x` by `bed_y` mm bed, or None.
pub fn first_off_bed(text: &[u8], bed_x: f64, bed_y: f64) -> Result<Option<(f64, f64)>, String> {
    let (mut x, mut y) = (0.0_f64, 0.0_f64);
    for line in text.split(|&b| is_line_break(b)) {
        let Some(words) = move_words(line) else { continue };
        if apply_words(words, &mut x, &mut y)? && (x < 0.0 || y < 0.0 || x > bed_x || y > bed_y) {
            return Ok(Some((x, y)));
        }
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scan(text: &str) -> Option<(f64, f64)> {
        first_off_bed(text.as_bytes(), 220.0, 220.0).unwrap()
    }

    #[test]
    fn in_bounds_moves_pass() {
        assert_eq!(scan("G1 X10 Y20 E1\nG0 X220 Y0\n"), None);
    }

    #[test]
    fn the_first_off_bed_move_is_reported_with_both_axes() {
        assert_eq!(scan("G1 X10 Y20\nG1 Y-1\nG1 X999\n"), Some((10.0, -1.0)));
    }

    #[test]
    fn only_g0_to_g3_at_the_line_start_count() {
        assert_eq!(scan("G10 X-5\nG92 X-5\nM104 X-5\n; G1 X-5\nG1X-5\n"), None);
        assert_eq!(scan(" \tg2 x-5\n"), Some((-5.0, 0.0)));
    }

    #[test]
    fn words_stop_at_a_semicolon_and_need_a_boundary() {
        assert_eq!(scan("G1 X5 ; X-5\nG1 X5Y-5\nG1 E1X-5\n"), None);
        assert_eq!(scan("G1 X5.Y-5\n"), Some((5.0, -5.0)));
    }

    #[test]
    fn the_last_word_of_a_line_wins() {
        assert_eq!(scan("G1 X-5 X5\n"), None);
    }

    #[test]
    fn number_forms_read_like_python() {
        assert_eq!(scan("G1 X .5 Y5.\n"), None);
        assert_eq!(scan("G1 X-.5\n"), Some((-0.5, 0.0)));
        assert_eq!(scan("G1 X- 5 Y-\n"), None);
    }

    #[test]
    fn every_python_line_break_splits() {
        assert_eq!(scan("M117 a\rG1 X-1\n"), Some((-1.0, 0.0)));
        assert_eq!(scan("M117 a\x1eG1 Y-1\n"), Some((0.0, -1.0)));
        assert_eq!(scan("G1 X1\x1fX-1\n"), Some((-1.0, 0.0)));
    }
}
