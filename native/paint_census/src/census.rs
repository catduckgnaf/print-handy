//! Census whole object spans selected by the Python 3MF reader. Mirror its byte
//! regexes, not an XML parser: intake validation remains the caller's job.
use std::collections::HashMap;

pub(crate) type Counts = (HashMap<usize, usize>, usize);

fn word(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

fn triangle_start(tag: &[u8]) -> bool {
    fn is_triangle(s: &[u8]) -> bool {
        s.strip_prefix(b"triangle")
            .is_some_and(|rest| rest.first().is_none_or(|&b| !word(b)))
    }
    if is_triangle(tag) {
        return true;
    }
    let end = tag.iter().take_while(|&&b| word(b)).count();
    end > 0 && tag.get(end) == Some(&b':') && is_triangle(&tag[end + 1..])
}

fn paint<'a>(tag: &'a [u8], prefix: &[u8]) -> Option<&'a [u8]> {
    for (i, _) in tag.iter().enumerate().filter(|(_, b)| **b == prefix[0]) {
        if let Some(rest) = tag[i..].strip_prefix(prefix) {
            let end = rest.iter().take_while(|b| b.is_ascii_hexdigit()).count();
            if end > 0 && rest.get(end) == Some(&b'"') {
                return Some(&rest[..end]);
            }
        }
    }
    None
}

fn nibble(byte: u8) -> u8 {
    match byte {
        b'0'..=b'9' => byte - b'0',
        b'A'..=b'F' => byte - b'A' + 10,
        b'a'..=b'f' => byte - b'a' + 10,
        _ => unreachable!("the paint matcher only accepts hex"),
    }
}

fn decode(code: &[u8], states: &mut HashMap<usize, usize>) -> Result<(), &'static str> {
    let mut nibbles = code.iter().rev().copied().map(nibble);
    let mut pending = [0u8; 64];
    let mut depth = 1;
    pending[0] = 1;
    while depth > 0 {
        let node = nibbles.next().ok_or("truncated paint_color")?;
        pending[depth - 1] -= 1;
        let children = node & 3;
        if children > 0 {
            // Have the reference handle unusual nesting rather than changing
            // its recursion/error behavior or allocating per node in Rust.
            if depth == pending.len() {
                return Err("paint census nesting limit");
            }
            pending[depth] = children + 1;
            depth += 1;
            continue;
        }
        let mut state = usize::from(node >> 2);
        if state == 3 {
            loop {
                let extra = nibbles.next().ok_or("truncated paint_color")?;
                state = state
                    .checked_add(usize::from(extra))
                    .ok_or("paint state overflow")?;
                if extra != 15 {
                    break;
                }
            }
        }
        *states.entry(state).or_default() += 1;
        while depth > 0 && pending[depth - 1] == 0 {
            depth -= 1;
        }
    }
    if nibbles.next().is_some() {
        Err("trailing paint_color data")
    } else {
        Ok(())
    }
}

pub(crate) fn mesh(mut data: &[u8]) -> Result<Counts, &'static str> {
    let mut states = HashMap::new();
    let mut unpainted = 0;
    while let Some(start) = data.iter().position(|&b| b == b'<') {
        data = &data[start + 1..];
        if !triangle_start(data) {
            continue;
        }
        let Some(end) = data.iter().position(|&b| b == b'>') else {
            break;
        };
        let tag = &data[..end];
        if let Some(code) = paint(tag, b"paint_color=\"").or_else(|| paint(tag, b"mmu_segmentation=\"")) {
            decode(code, &mut states)?;
        } else {
            unpainted += 1;
        }
        data = &data[end + 1..];
    }
    Ok((states, unpainted))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn precedences_and_split_leaves() {
        let data = br#"<p:triangles><p:triangle paint_color="0fC" slic3rpe:mmu_segmentation="8"/><p:triangle slic3rpe:mmu_segmentation="849"/><p:triangle/></p:triangles>"#;
        assert_eq!(mesh(data), Ok((HashMap::from([(1, 1), (2, 1), (18, 1)]), 1)));
    }

    #[test]
    fn errors_match_the_reference() {
        assert_eq!(mesh(br#"<triangle paint_color="FC"/>"#), Err("truncated paint_color"));
        assert_eq!(mesh(br#"<triangle paint_color="44"/>"#), Err("trailing paint_color data"));
    }
}
