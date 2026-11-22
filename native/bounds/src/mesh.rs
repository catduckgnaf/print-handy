//! Native mesh kernel for large mesh intake (#1790).
//!
//! The Python code stays the permanent fallback and the test oracle; the
//! dispatcher in `print_handy/core/mesh_native.py` uses this kernel only
//! when the module imports, `MESH_KERNEL_VERSION` matches and a golden
//! self-check passes, and only for input it can hand over exactly (typed
//! float64 storage with every coordinate finite). Every function returns
//! `Err` for anything outside that contract, and the dispatcher then runs
//! the Python code for that call. The contracts, all pinned by
//! `tests/pipeline/test_native_mesh.py`:
//!
//! * A mesh is a triangle soup (nine coordinates per triangle) or indexed
//!   storage (vertices plus three vertex indices per triangle), optionally
//!   followed by a chain of transforms applied per point in order, inner
//!   first, with the exact operation order of the Python lambdas:
//!   `rotz(c, s)` is `(c*x - s*y, s*x + c*y, z)`, `translate(dx, dy, dz)`
//!   is `(x+dx, y+dy, z+dz)`, and `affine(t0..t11)` is
//!   `geometry_layout.apply` (`t0*x + t3*y + t6*z + t9`, ...). Rust does
//!   not contract `a*b + c` into a fused multiply-add, so every value
//!   rounds as the Python float arithmetic does.
//! * `round6` is Python's `round(x, 6)`: the correctly rounded decimal with
//!   ties to even on the exact binary value.
//! * Topology is `print_handy.core.compact_topology.mesh_topology_compact`:
//!   vertices weld on their `round6` coordinates (`-0.0` equals `0.0`), a
//!   triangle is degenerate when two welded corners coincide or its squared
//!   doubled area (`c0*c0 + c1*c1 + c2*c2`, from the unrounded corners) is
//!   `<= 1e-18`, degenerate triangles take no part in adjacency, shells are
//!   triangles joined through shared edges, an edge used once is a boundary
//!   edge and more than twice a non-manifold edge, both ends of such an edge
//!   are singular, a closed edge whose two uses run the same direction is a
//!   winding inconsistency, and a vertex whose corners form more than one
//!   fan across closed edges is singular too.
//! * Bounds and sums follow the scalar Python loops: bounds start at the
//!   first point and move only on a strictly smaller or larger value (so
//!   the first of equal values wins, which decides the sign of a zero), and
//!   area and signed volume add one triangle at a time in triangle order,
//!   from 0.0, with the scalar per-triangle formulas.
//! * The 3MF text is `geometry_layout.write_geometry_only_3mf`'s loop: each
//!   corner's key is its three `round6` values; a new key appends a vertex
//!   (keeping the first corner's values, signed zeros included) and every
//!   corner refers to its key's vertex; vertices print with `{:.6f}`.
//! * STL parsing is `orca_adapter.read_stl_triangles`: binary when the
//!   data is at least 84 bytes and exactly `84 + 50 * count` long (float32
//!   corners widened to float64), otherwise ASCII (only pure-ASCII data
//!   with every token in the plain decimal grammar; anything else is left
//!   to Python's regex and `float()`).
//!
//! No `unsafe` code.

use std::collections::HashMap;
use std::fmt::Write as _;
use std::hash::{BuildHasherDefault, Hasher};

/// Version handshake with the Python dispatcher. Bump on any semantic change.
pub const MESH_KERNEL_VERSION: u32 = 1;

// ---------------------------------------------------------------------------
// Hashing (std only): a multiply-rotate word hasher with a murmur finalizer,
// for keys that are the raw bits of float coordinates.
// ---------------------------------------------------------------------------

#[derive(Default)]
pub struct WordHasher(u64);

impl Hasher for WordHasher {
    fn finish(&self) -> u64 {
        let mut h = self.0;
        h ^= h >> 33;
        h = h.wrapping_mul(0xff51_afd7_ed55_8ccd);
        h ^= h >> 33;
        h = h.wrapping_mul(0xc4ce_b9fe_1a85_ec53);
        h ^= h >> 33;
        h
    }

    fn write(&mut self, bytes: &[u8]) {
        for chunk in bytes.chunks(8) {
            let mut word = [0u8; 8];
            word[..chunk.len()].copy_from_slice(chunk);
            self.write_u64(u64::from_le_bytes(word));
        }
    }

    fn write_u64(&mut self, x: u64) {
        self.0 = (self.0.rotate_left(5) ^ x).wrapping_mul(0x517c_c1b7_2722_0a95);
    }

    fn write_usize(&mut self, x: usize) {
        self.write_u64(x as u64);
    }
}

type FastMap<K, V> = HashMap<K, V, BuildHasherDefault<WordHasher>>;

fn fast_map<K, V>(capacity: usize) -> FastMap<K, V> {
    HashMap::with_capacity_and_hasher(capacity, BuildHasherDefault::default())
}

/// Exact bits of a point (signed zeros distinct).
fn exact_key(p: [f64; 3]) -> [u64; 3] {
    [p[0].to_bits(), p[1].to_bits(), p[2].to_bits()]
}

/// Bits of a point with `-0.0` folded onto `0.0`, so keys compare like
/// Python floats (no NaN reaches here: inputs are checked finite).
fn value_key(p: [f64; 3]) -> [u64; 3] {
    [(p[0] + 0.0).to_bits(), (p[1] + 0.0).to_bits(), (p[2] + 0.0).to_bits()]
}

// ---------------------------------------------------------------------------
// Python round(x, 6)
// ---------------------------------------------------------------------------

/// Python's `round(x, 6)` through decimal formatting: Rust's fixed
/// precision formatting is exact with ties to even, and the parse back is
/// correctly rounded, like CPython's dtoa round trip.
fn round6_slow(x: f64, buf: &mut String) -> f64 {
    buf.clear();
    // Writing to a String cannot fail.
    let _ = write!(buf, "{:.6}", x);
    buf.parse::<f64>().unwrap_or(x)
}

/// Python's `round(x, 6)` for a finite `x`.
///
/// Fast path for `|x| < 4e9`: the exact product `x * 1e6` is `p + e`
/// (`e` from a fused multiply-add, exact), its nearest integer `n` with
/// ties to even is decided from `p`'s nearest integer and the sign of `e`,
/// and `n / 1e6` is then the correctly rounded value of the decimal
/// `n * 10**-6`, which is what CPython's round trip returns.
pub fn round6(x: f64, buf: &mut String) -> f64 {
    if !x.is_finite() || x == 0.0 {
        return x;
    }
    let ax = x.abs();
    if ax >= 4_503_599_627_370_496.0 {
        // 2**52 and above: an integer already, so every decimal place is zero.
        return x;
    }
    if ax >= 4.0e9 {
        return round6_slow(x, buf);
    }
    let p = x * 1.0e6;
    let e = x.mul_add(1.0e6, -p);
    let r = p.round_ties_even();
    // p - r is exact (|p - r| <= 0.5 and both are multiples of ulp(p)).
    let h = p - r;
    let n = if h == 0.5 {
        if e > 0.0 {
            r + 1.0
        } else if e < 0.0 {
            r
        } else {
            r // an exact tie: round_ties_even already picked the even one
        }
    } else if h == -0.5 {
        if e < 0.0 {
            r - 1.0
        } else {
            r
        }
    } else {
        r
    };
    n / 1.0e6
}

// ---------------------------------------------------------------------------
// Meshes and transforms
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, PartialEq)]
pub enum Op {
    RotZ { c: f64, s: f64 },
    Translate { d: [f64; 3] },
    Affine { t: [f64; 12] },
}

impl Op {
    pub fn from_parts(kind: &str, params: &[f64]) -> Result<Op, String> {
        match (kind, params.len()) {
            ("rotz", 2) => Ok(Op::RotZ { c: params[0], s: params[1] }),
            ("translate", 3) => Ok(Op::Translate { d: [params[0], params[1], params[2]] }),
            ("affine", 12) => {
                let mut t = [0.0; 12];
                t.copy_from_slice(params);
                Ok(Op::Affine { t })
            }
            _ => Err(format!("unknown transform {kind:?} with {} values", params.len())),
        }
    }

    #[inline]
    fn apply(&self, p: [f64; 3]) -> [f64; 3] {
        let [x, y, z] = p;
        match self {
            Op::RotZ { c, s } => [c * x - s * y, s * x + c * y, z],
            Op::Translate { d } => [x + d[0], y + d[1], z + d[2]],
            Op::Affine { t } => [
                t[0] * x + t[3] * y + t[6] * z + t[9],
                t[1] * x + t[4] * y + t[7] * z + t[10],
                t[2] * x + t[5] * y + t[8] * z + t[11],
            ],
        }
    }
}

/// A triangle mesh view: a soup (`faces` None, nine values per triangle) or
/// indexed storage, plus the transforms applied to every point.
pub struct Mesh<'a> {
    vertices: &'a [f64],
    faces: Option<&'a [i64]>,
    ops: &'a [Op],
}

impl<'a> Mesh<'a> {
    /// Check the storage shape and indices. Coordinates are checked finite
    /// after the transforms by `triangle`, which every caller reads through.
    pub fn new(vertices: &'a [f64], faces: Option<&'a [i64]>, ops: &'a [Op]) -> Result<Mesh<'a>, String> {
        match faces {
            None => {
                if vertices.len() % 9 != 0 {
                    return Err("triangle soup length is not a multiple of nine".into());
                }
            }
            Some(f) => {
                if vertices.len() % 3 != 0 || f.len() % 3 != 0 {
                    return Err("indexed storage is not three values per row".into());
                }
                let nv = (vertices.len() / 3) as i64;
                if f.iter().any(|&i| i < 0 || i >= nv) {
                    return Err("triangle index outside the vertex table".into());
                }
            }
        }
        Ok(Mesh { vertices, faces, ops })
    }

    #[allow(clippy::len_without_is_empty)]
    pub fn len(&self) -> usize {
        match self.faces {
            None => self.vertices.len() / 9,
            Some(f) => f.len() / 3,
        }
    }

    #[inline]
    fn raw_point(&self, tri: usize, corner: usize) -> [f64; 3] {
        let base = match self.faces {
            None => tri * 9 + corner * 3,
            Some(f) => f[tri * 3 + corner] as usize * 3,
        };
        [self.vertices[base], self.vertices[base + 1], self.vertices[base + 2]]
    }

    #[inline]
    fn point(&self, tri: usize, corner: usize) -> [f64; 3] {
        let mut p = self.raw_point(tri, corner);
        for op in self.ops {
            p = op.apply(p);
        }
        p
    }

    /// One triangle's transformed corners, or Err when any value is not
    /// finite (Python's NaN and infinity handling stays authoritative).
    #[inline]
    pub fn triangle(&self, tri: usize) -> Result<[[f64; 3]; 3], String> {
        let t = [self.point(tri, 0), self.point(tri, 1), self.point(tri, 2)];
        if t.iter().flatten().all(|v| v.is_finite()) {
            Ok(t)
        } else {
            Err("non-finite coordinate".into())
        }
    }
}

/// The transformed soup as nine values per triangle.
pub fn transform(mesh: &Mesh) -> Result<Vec<f64>, String> {
    let mut out = Vec::with_capacity(mesh.len() * 9);
    for tri in 0..mesh.len() {
        for p in mesh.triangle(tri)? {
            out.extend_from_slice(&p);
        }
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Bounds, area and signed volume
// ---------------------------------------------------------------------------

#[derive(Debug, PartialEq)]
pub struct Measure {
    pub lo: [f64; 3],
    pub hi: [f64; 3],
    pub area: f64,
    pub signed_volume: f64,
}

/// Bounds, area and signed volume with the scalar Python loops' order.
pub fn measure(mesh: &Mesh) -> Result<Measure, String> {
    if mesh.len() == 0 {
        return Err("empty triangle table".into());
    }
    let first = mesh.triangle(0)?[0];
    let mut lo = first;
    let mut hi = first;
    let mut area = 0.0_f64;
    let mut volume = 0.0_f64;
    for index in 0..mesh.len() {
        let tri = mesh.triangle(index)?;
        for p in &tri {
            for axis in 0..3 {
                if p[axis] < lo[axis] {
                    lo[axis] = p[axis];
                }
                if p[axis] > hi[axis] {
                    hi[axis] = p[axis];
                }
            }
        }
        let [ax, ay, az] = tri[0];
        let [bx, by, bz] = tri[1];
        let [cx, cy, cz] = tri[2];
        let (ux, uy, uz) = (bx - ax, by - ay, bz - az);
        let (vx, vy, vz) = (cx - ax, cy - ay, cz - az);
        let nx = uy * vz - uz * vy;
        let ny = uz * vx - ux * vz;
        let nz = ux * vy - uy * vx;
        area += 0.5 * (nx * nx + ny * ny + nz * nz).sqrt();
        volume += (ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)) / 6.0;
    }
    Ok(Measure { lo, hi, area, signed_volume: volume })
}

// ---------------------------------------------------------------------------
// Welding
// ---------------------------------------------------------------------------

/// Welds points to vertex ids in first-occurrence order. With `round`, the
/// key is the point's `round6` coordinates and the stored vertex is the
/// first corner's rounded values; without it the key is the exact value.
/// Exact repeats skip the rounding through a bits cache.
pub struct Welder {
    round: bool,
    exact: FastMap<[u64; 3], u32>,
    keyed: FastMap<[u64; 3], u32>,
    pub vertices: Vec<[f64; 3]>,
    buf: String,
}

impl Welder {
    pub fn new(round: bool, capacity: usize) -> Welder {
        Welder {
            round,
            exact: fast_map(capacity),
            keyed: fast_map(if round { capacity } else { 0 }),
            vertices: Vec::with_capacity(capacity),
            buf: String::with_capacity(48),
        }
    }

    pub fn id(&mut self, p: [f64; 3]) -> u32 {
        let bits = exact_key(p);
        if let Some(&id) = self.exact.get(&bits) {
            return id;
        }
        let id = if self.round {
            let r = [round6(p[0], &mut self.buf), round6(p[1], &mut self.buf), round6(p[2], &mut self.buf)];
            let next = self.vertices.len() as u32;
            let id = *self.keyed.entry(value_key(r)).or_insert(next);
            if id == next {
                self.vertices.push(r);
            }
            id
        } else {
            // Exact welding still treats -0.0 as 0.0, like Python equality.
            let next = self.vertices.len() as u32;
            let id = *self.keyed.entry(value_key(p)).or_insert(next);
            if id == next {
                self.vertices.push(p);
            }
            id
        };
        self.exact.insert(bits, id);
        id
    }
}

/// An indexed mesh: welded vertices and three vertex ids per triangle.
pub fn weld(mesh: &Mesh, round: bool) -> Result<(Vec<[f64; 3]>, Vec<u32>), String> {
    let mut welder = Welder::new(round, mesh.len() / 2 + 16);
    let mut faces = Vec::with_capacity(mesh.len() * 3);
    for tri in 0..mesh.len() {
        for p in mesh.triangle(tri)? {
            faces.push(welder.id(p));
        }
    }
    Ok((welder.vertices, faces))
}

// ---------------------------------------------------------------------------
// Topology
// ---------------------------------------------------------------------------

#[derive(Debug, PartialEq, Eq, Default)]
pub struct Topology {
    pub triangles: usize,
    pub shells: usize,
    pub boundary_edges: usize,
    pub non_manifold_edges: usize,
    pub singular_vertices: usize,
    pub winding_inconsistencies: usize,
    pub degenerate_triangles: usize,
}

struct Dsu {
    parent: Vec<u32>,
}

impl Dsu {
    fn new(n: usize) -> Dsu {
        Dsu { parent: (0..n as u32).collect() }
    }

    fn find(&mut self, mut i: u32) -> u32 {
        while self.parent[i as usize] != i {
            let grand = self.parent[self.parent[i as usize] as usize];
            self.parent[i as usize] = grand;
            i = grand;
        }
        i
    }

    fn union(&mut self, a: u32, b: u32) {
        let (ra, rb) = (self.find(a), self.find(b));
        if ra != rb {
            let (low, high) = if ra < rb { (ra, rb) } else { (rb, ra) };
            self.parent[high as usize] = low;
        }
    }
}

pub fn topology(mesh: &Mesh) -> Result<Topology, String> {
    let total = mesh.len();
    if total > (u32::MAX / 4) as usize {
        return Err("mesh too large for the native topology".into());
    }
    let mut welder = Welder::new(true, total / 2 + 16);
    let mut faces: Vec<[u32; 3]> = Vec::with_capacity(total);
    let mut degenerate = 0usize;
    for index in 0..total {
        let tri = mesh.triangle(index)?;
        let ids = [welder.id(tri[0]), welder.id(tri[1]), welder.id(tri[2])];
        let (a, b, c) = (tri[0], tri[1], tri[2]);
        let u = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
        let v = [c[0] - a[0], c[1] - a[1], c[2] - a[2]];
        let cross = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]];
        let sumsq = cross[0] * cross[0] + cross[1] * cross[1] + cross[2] * cross[2];
        let distinct = ids[0] != ids[1] && ids[1] != ids[2] && ids[0] != ids[2];
        if distinct && sumsq > 1e-18 {
            faces.push(ids);
        } else {
            degenerate += 1;
        }
    }
    let nf = faces.len();
    let nv = welder.vertices.len();
    drop(welder);

    // Edges keep their corner: corner 3f+k runs from faces[f][k] to faces[f][k+1].
    let mut edges: Vec<(u64, u32)> = Vec::with_capacity(nf * 3);
    for (f, face) in faces.iter().enumerate() {
        for k in 0..3 {
            let (a, b) = (face[k], face[(k + 1) % 3]);
            let key = ((a.min(b) as u64) << 32) | a.max(b) as u64;
            edges.push((key, (f * 3 + k) as u32));
        }
    }
    edges.sort_unstable();

    let corner_from = |corner: u32| faces[(corner / 3) as usize][(corner % 3) as usize];
    let corner_to = |corner: u32| faces[(corner / 3) as usize][((corner % 3 + 1) % 3) as usize];
    let next_corner = |corner: u32| (corner / 3) * 3 + (corner % 3 + 1) % 3;

    let mut shells = Dsu::new(nf);
    let mut corners = Dsu::new(nf * 3);
    let mut singular = vec![false; nv];
    let (mut boundary, mut nonmanifold, mut winding) = (0usize, 0usize, 0usize);
    let mut start = 0;
    while start < edges.len() {
        let key = edges[start].0;
        let mut stop = start + 1;
        while stop < edges.len() && edges[stop].0 == key {
            stop += 1;
        }
        let first = edges[start].1;
        for item in &edges[start + 1..stop] {
            shells.union(first / 3, item.1 / 3);
        }
        let count = stop - start;
        if count != 2 {
            singular[corner_from(first) as usize] = true;
            singular[corner_to(first) as usize] = true;
            if count == 1 {
                boundary += 1;
            } else {
                nonmanifold += 1;
            }
        } else {
            let other = edges[start + 1].1;
            let same = (corner_from(first) < corner_to(first)) == (corner_from(other) < corner_to(other));
            if same {
                winding += 1;
                corners.union(first, other);
                corners.union(next_corner(first), next_corner(other));
            } else {
                corners.union(first, next_corner(other));
                corners.union(next_corner(first), other);
            }
        }
        start = stop;
    }

    // A vertex whose corners fall in more than one fan is singular.
    let mut fan = vec![u32::MAX; nv];
    for corner in 0..(nf * 3) as u32 {
        let vertex = corner_from(corner) as usize;
        let root = corners.find(corner);
        if fan[vertex] == u32::MAX {
            fan[vertex] = root;
        } else if fan[vertex] != root {
            singular[vertex] = true;
        }
    }
    let shell_count = (0..nf as u32).filter(|&f| shells.find(f) == f).count();
    Ok(Topology {
        triangles: nf + degenerate,
        shells: shell_count,
        boundary_edges: boundary,
        non_manifold_edges: nonmanifold,
        singular_vertices: singular.iter().filter(|&&s| s).count(),
        winding_inconsistencies: winding,
        degenerate_triangles: degenerate,
    })
}

// ---------------------------------------------------------------------------
// 3MF mesh text
// ---------------------------------------------------------------------------

fn push_uint(out: &mut String, mut n: u64) {
    let mut digits = [0u8; 20];
    let mut i = digits.len();
    loop {
        i -= 1;
        digits[i] = b'0' + (n % 10) as u8;
        n /= 10;
        if n == 0 {
            break;
        }
    }
    for &d in &digits[i..] {
        out.push(d as char);
    }
}

/// Python's `f"{v:.6f}"` for a value `round6` produced. Below 1e6 the value
/// is the double nearest `n / 10**6` for an integer `n` that `v * 1e6`
/// recovers exactly, and `{:.6f}` prints exactly `n`'s digits; larger
/// values use the general (exact) formatter.
fn push_fixed6(out: &mut String, v: f64) {
    if v.abs() >= 1.0e6 {
        let _ = write!(out, "{:.6}", v);
        return;
    }
    let n = (v * 1.0e6).round();
    if v.is_sign_negative() {
        out.push('-');
    }
    let m = n.abs() as u64;
    push_uint(out, m / 1_000_000);
    out.push('.');
    let mut digits = [b'0'; 6];
    let mut rest = m % 1_000_000;
    for d in digits.iter_mut().rev() {
        *d = b'0' + (rest % 10) as u8;
        rest /= 10;
    }
    for d in digits {
        out.push(d as char);
    }
}

/// The `<vertex>` lines and the `<triangle>` lines (each set joined by
/// `\n`, no trailing newline) of one object in
/// `write_geometry_only_3mf`'s layout.
pub fn threemf_text(mesh: &Mesh) -> Result<(String, String), String> {
    let mut welder = Welder::new(true, mesh.len() / 2 + 16);
    let mut ids = Vec::with_capacity(mesh.len() * 3);
    for tri in 0..mesh.len() {
        for p in mesh.triangle(tri)? {
            ids.push(welder.id(p));
        }
    }
    let mut vertices = String::with_capacity(welder.vertices.len() * 64);
    for (i, v) in welder.vertices.iter().enumerate() {
        if i > 0 {
            vertices.push('\n');
        }
        vertices.push_str("          <vertex x=\"");
        push_fixed6(&mut vertices, v[0]);
        vertices.push_str("\" y=\"");
        push_fixed6(&mut vertices, v[1]);
        vertices.push_str("\" z=\"");
        push_fixed6(&mut vertices, v[2]);
        vertices.push_str("\"/>");
    }
    let mut triangles = String::with_capacity(ids.len() / 3 * 52);
    for (i, t) in ids.chunks_exact(3).enumerate() {
        if i > 0 {
            triangles.push('\n');
        }
        triangles.push_str("          <triangle v1=\"");
        push_uint(&mut triangles, t[0] as u64);
        triangles.push_str("\" v2=\"");
        push_uint(&mut triangles, t[1] as u64);
        triangles.push_str("\" v3=\"");
        push_uint(&mut triangles, t[2] as u64);
        triangles.push_str("\"/>");
    }
    Ok((vertices, triangles))
}

// ---------------------------------------------------------------------------
// STL
// ---------------------------------------------------------------------------

#[derive(Debug, PartialEq)]
pub enum StlKind {
    Binary,
    Ascii,
}

fn is_py_space(b: u8) -> bool {
    // Python str `\s` restricted to ASCII: \t \n \v \f \r, \x1c-\x1f, space.
    matches!(b, 0x09..=0x0d | 0x1c..=0x1f | 0x20)
}

/// A token in the plain decimal grammar `[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?`,
/// parsed with correct rounding (Python `float()` agrees on this grammar).
fn parse_plain_float(tok: &[u8]) -> Option<f64> {
    let mut i = 0;
    if i < tok.len() && (tok[i] == b'+' || tok[i] == b'-') {
        i += 1;
    }
    let int_start = i;
    while i < tok.len() && tok[i].is_ascii_digit() {
        i += 1;
    }
    let int_digits = i - int_start;
    let mut frac_digits = 0;
    if i < tok.len() && tok[i] == b'.' {
        i += 1;
        let frac_start = i;
        while i < tok.len() && tok[i].is_ascii_digit() {
            i += 1;
        }
        frac_digits = i - frac_start;
    }
    if int_digits == 0 && frac_digits == 0 {
        return None;
    }
    let mantissa_end = i;
    if i < tok.len() && (tok[i] == b'e' || tok[i] == b'E') {
        i += 1;
        if i < tok.len() && (tok[i] == b'+' || tok[i] == b'-') {
            i += 1;
        }
        let exp_start = i;
        while i < tok.len() && tok[i].is_ascii_digit() {
            i += 1;
        }
        if i == exp_start {
            return None;
        }
    }
    if i != tok.len() {
        return None;
    }
    let text = std::str::from_utf8(tok).ok()?;
    // Rust rejects "1." and "1.e5"; insert the zero Python implies.
    if frac_digits == 0 && tok[..mantissa_end].ends_with(b".") {
        let normalized = format!("{}0{}", &text[..mantissa_end], &text[mantissa_end..]);
        return normalized.parse::<f64>().ok();
    }
    text.parse::<f64>().ok()
}

/// The maximal non-space run starting at `i` (`\S+`), or None when empty.
fn token(data: &[u8], i: usize) -> Option<usize> {
    let mut j = i;
    while j < data.len() && !is_py_space(data[j]) {
        j += 1;
    }
    (j > i).then_some(j)
}

fn spaces(data: &[u8], i: usize) -> Option<usize> {
    let mut j = i;
    while j < data.len() && is_py_space(data[j]) {
        j += 1;
    }
    (j > i).then_some(j)
}

/// `re.finditer(r'vertex\s+(\S+)\s+(\S+)\s+(\S+)')` over ASCII text,
/// with `float()` on each group.
fn parse_ascii_stl(data: &[u8]) -> Result<Vec<f64>, String> {
    if !data.is_ascii() {
        return Err("non-ASCII STL text".into());
    }
    let mut coords = Vec::new();
    let mut i = 0;
    'scan: while i + 6 <= data.len() {
        if &data[i..i + 6] != b"vertex" {
            i += 1;
            continue;
        }
        let mut j = i + 6;
        let mut values = [0.0; 3];
        for value in &mut values {
            let Some(after_space) = spaces(data, j) else {
                i += 1;
                continue 'scan;
            };
            let Some(end) = token(data, after_space) else {
                i += 1;
                continue 'scan;
            };
            *value = parse_plain_float(&data[after_space..end])
                .ok_or_else(|| "STL number outside the plain decimal grammar".to_string())?;
            j = end;
        }
        coords.extend_from_slice(&values);
        i = j;
    }
    if coords.is_empty() || (coords.len() / 3) % 3 != 0 {
        return Err("not a readable STL".into());
    }
    Ok(coords)
}

pub fn parse_stl(data: &[u8]) -> Result<(StlKind, Vec<f64>), String> {
    if data.len() >= 84 {
        let count = u32::from_le_bytes([data[80], data[81], data[82], data[83]]) as usize;
        if data.len() as u128 == 84 + 50 * count as u128 {
            let mut coords = Vec::with_capacity(count * 9);
            for row in data[84..].chunks_exact(50) {
                for k in 3..12 {
                    let b = &row[k * 4..k * 4 + 4];
                    coords.push(f32::from_le_bytes([b[0], b[1], b[2], b[3]]) as f64);
                }
            }
            return Ok((StlKind::Binary, coords));
        }
    }
    parse_ascii_stl(data).map(|coords| (StlKind::Ascii, coords))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tetra() -> Vec<f64> {
        let p = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]];
        let faces = [[0, 2, 1], [0, 1, 3], [1, 2, 3], [0, 3, 2]];
        faces.iter().flat_map(|f| f.iter().flat_map(|&i| p[i])).collect()
    }

    #[test]
    fn round6_matches_formatting_and_python_ties() {
        let mut buf = String::new();
        // Exact binary ties at the sixth decimal: Python rounds half to even.
        assert_eq!(round6(0.0078125, &mut buf), 0.007812);
        assert_eq!(round6(0.0234375, &mut buf), 0.023438);
        assert_eq!(round6(-0.0078125, &mut buf), -0.007812);
        assert_eq!(round6(2.5e-7, &mut buf), 0.0);
        assert!(round6(-1e-7, &mut buf).is_sign_negative());
        assert_eq!(round6(1.23456789, &mut buf), 1.234568);
        assert_eq!(round6(1e300, &mut buf), 1e300);
        let mut state = 0x1234_5678_9abc_def0_u64;
        for _ in 0..200_000 {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            let mantissa = (state >> 11) as f64 / (1u64 << 53) as f64;
            let scale = [1e-7, 1e-3, 1.0, 37.0, 1e3, 1e6, 3e9][(state % 7) as usize];
            let mut x = (mantissa - 0.5) * scale;
            if state % 5 == 0 {
                // Values on the 1/128 grid hit exact decimal ties.
                x = ((state >> 20) % 100_000) as f64 / 128.0 - 300.0;
            }
            assert_eq!(round6(x, &mut buf).to_bits(), round6_slow(x, &mut buf).to_bits(), "{x:e}");
        }
    }

    #[test]
    fn fixed6_prints_like_the_general_formatter() {
        let mut buf = String::new();
        let mut state = 0x9e37_79b9_7f4a_7c15_u64;
        for i in 0..300_000 {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            let mantissa = (state >> 11) as f64 / (1u64 << 53) as f64;
            let scale = [1e-6, 1e-3, 1.0, 250.0, 9.9e5, 2e6][(state % 6) as usize];
            let mut x = (mantissa - 0.5) * scale;
            if i % 97 == 0 {
                x = -0.0;
            }
            let v = round6(x, &mut buf);
            let mut fast = String::new();
            push_fixed6(&mut fast, v);
            assert_eq!(fast, format!("{:.6}", v), "{v:e}");
        }
    }

    #[test]
    fn tetrahedron_is_closed_and_measured() {
        let soup = tetra();
        let mesh = Mesh::new(&soup, None, &[]).unwrap();
        let topo = topology(&mesh).unwrap();
        assert_eq!(topo, Topology { triangles: 4, shells: 1, ..Default::default() });
        let m = measure(&mesh).unwrap();
        assert_eq!(m.lo, [0.0, 0.0, 0.0]);
        assert_eq!(m.hi, [1.0, 1.0, 1.0]);
        assert!((m.signed_volume.abs() - 1.0 / 6.0).abs() < 1e-15);
    }

    #[test]
    fn open_and_touching_meshes_report_their_defects() {
        let mut soup = tetra();
        soup.truncate(27); // drop one face: three boundary edges
        let mesh = Mesh::new(&soup, None, &[]).unwrap();
        let topo = topology(&mesh).unwrap();
        assert_eq!(topo.boundary_edges, 3);
        assert_eq!(topo.singular_vertices, 3);
        // Two tetrahedra sharing one vertex: closed edges, one singular vertex.
        let mut two = tetra();
        let shifted: Vec<f64> = tetra().iter().map(|v| -v).collect();
        two.extend(shifted);
        let mesh = Mesh::new(&two, None, &[]).unwrap();
        let topo = topology(&mesh).unwrap();
        assert_eq!(topo.shells, 2);
        assert_eq!(topo.boundary_edges, 0);
        assert_eq!(topo.singular_vertices, 1);
        assert_eq!(topo.winding_inconsistencies, 0);
    }

    #[test]
    fn transforms_keep_python_operation_order() {
        let soup = tetra();
        let ops = [Op::RotZ { c: 0.6, s: 0.8 }, Op::Translate { d: [1.0, 2.0, 3.0] }];
        let mesh = Mesh::new(&soup, None, &ops).unwrap();
        let out = transform(&mesh).unwrap();
        let (x, y, z) = (soup[3], soup[4], soup[5]);
        assert_eq!(out[3], (0.6 * x - 0.8 * y) + 1.0);
        assert_eq!(out[4], (0.8 * x + 0.6 * y) + 2.0);
        assert_eq!(out[5], z + 3.0);
    }

    #[test]
    fn threemf_text_welds_on_rounded_keys_and_keeps_first_zero_sign() {
        let soup = vec![-0.0, 0.0, 0.0, 1.0000001, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0];
        let mesh = Mesh::new(&soup, None, &[]).unwrap();
        let (vertices, triangles) = threemf_text(&mesh).unwrap();
        assert_eq!(
            vertices,
            "          <vertex x=\"-0.000000\" y=\"0.000000\" z=\"0.000000\"/>\n\
             \x20         <vertex x=\"1.000000\" y=\"0.000000\" z=\"0.000000\"/>\n\
             \x20         <vertex x=\"0.000000\" y=\"1.000000\" z=\"0.000000\"/>\n\
             \x20         <vertex x=\"0.000000\" y=\"0.000000\" z=\"1.000000\"/>"
        );
        assert_eq!(
            triangles,
            "          <triangle v1=\"0\" v2=\"1\" v3=\"2\"/>\n          <triangle v1=\"0\" v2=\"1\" v3=\"3\"/>"
        );
    }

    #[test]
    fn ascii_stl_follows_the_python_regex() {
        let text = b"solid x\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1. 0 0\n  vertex\t0 1.5e0 -0\nendloop\nendfacet\nendsolid";
        let (kind, coords) = parse_stl(text).unwrap();
        assert_eq!(kind, StlKind::Ascii);
        assert_eq!(coords, vec![0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.5, -0.0]);
        assert!(parse_stl(b"vertex 1 2 inf\nvertex 0 0 0\nvertex 0 0 0").is_err());
        assert!(parse_stl(b"vertex 1 2\n").is_err());
    }

    #[test]
    fn binary_stl_widens_float32() {
        let mut data = vec![0u8; 80];
        data.extend_from_slice(&1u32.to_le_bytes());
        for v in [0.0f32, 0.0, 1.0, 0.1, 0.2, 0.3, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0] {
            data.extend_from_slice(&v.to_le_bytes());
        }
        data.extend_from_slice(&[0, 0]);
        let (kind, coords) = parse_stl(&data).unwrap();
        assert_eq!(kind, StlKind::Binary);
        assert_eq!(coords[0], 0.1f32 as f64);
        assert_eq!(coords.len(), 9);
    }
}
