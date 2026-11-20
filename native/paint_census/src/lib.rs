use std::collections::HashMap;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

mod census;

#[pyfunction(name = "census")]
fn census_member(
    py: Python<'_>,
    model: &[u8],
    blocks: HashMap<String, (usize, usize)>,
) -> PyResult<HashMap<String, census::Counts>> {
    py.detach(|| {
        blocks
            .into_iter()
            .map(|(id, (start, end))| {
                let body = model
                    .get(start..end)
                    .ok_or_else(|| PyValueError::new_err("invalid paint census object span"))?;
                let counts = census::mesh(body).map_err(PyValueError::new_err)?;
                Ok((id, counts))
            })
            .collect()
    })
}

#[pymodule]
fn _paint_census_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("API_VERSION", 1)?;
    module.add_function(wrap_pyfunction!(census_member, module)?)
}
