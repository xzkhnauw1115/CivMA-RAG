"""
FEniCS 2019.1.0 golden recipe for reference bridge thermal gradient task.

Purpose:
- Stable snippets for prescribed temperature-gradient thermoelastic static analysis.
- Avoid using f_th = -div(sigma_th) on DG0 fields.
- Keep the project initial-stress convention:
  L = body + thermal - initial_stress.

This is a recipe, not a complete bridge model. Coder should adapt these
functions inside temp_scripts/fenics_drafts/current_fenics_script.py.
"""

from dolfin import *
from math import exp


E_c = 3.45e10
nu = 0.20
E_s = 2.0e11
rho_x = 0.0102
rho_y = 0.0015
rho_z = 0.0012
alpha_eff = 1.02e-5
Z_TOP = 131.0


def z_bottom_bridge(x):
    """User-provided variable bottom elevation for the 72.5+135+72.5 m bridge."""
    if x <= 72.5:
        return 126.5 - 6.5 * (1.0 - x / 72.5) ** 2
    if x <= 207.5:
        return 120.0 + 3.0 * (2.0 * abs(x - 140.0) / 135.0) ** 2
    xr = 280.0 - x
    return 126.5 - 6.5 * (1.0 - xr / 72.5) ** 2


def classify_thermal_gradient_layer(x, z):
    """Return 0 void/outside, 1 bottom, 2 middle, 3 top for main beam cells."""
    zb = z_bottom_bridge(x)
    if z < zb or z > Z_TOP:
        return 0
    if z >= Z_TOP - 2.0:
        return 3
    if z <= zb + 2.0:
        return 1
    return 2


def thermal_gradient_delta_T(z, is_beam):
    """Prescribed temperature increment. Pier/void cells use dT=0."""
    if not is_beam:
        return 0.0
    return 15.0 * exp(-2.0 * (Z_TOP - z))


def fill_thermal_gradient_dg0_fields(mesh, DG0):
    """Stable DG0 fill pattern using cell.index() on project structured meshes."""
    delta_T = Function(DG0)
    layer_tag = Function(DG0)
    s0_xx = Function(DG0)

    dT_values = delta_T.vector().get_local()
    layer_values = layer_tag.vector().get_local()
    s0_values = s0_xx.vector().get_local()

    counts = {
        "top_layer_cells": 0,
        "middle_layer_cells": 0,
        "bottom_layer_cells": 0,
        "thermal_loaded_cells": 0,
        "top_prestress_cells": 0,
        "bottom_prestress_cells": 0,
    }

    for cell in cells(mesh):
        mp = cell.midpoint()
        x, z = mp.x(), mp.z()
        idx = cell.index()

        layer = classify_thermal_gradient_layer(x, z)
        layer_values[idx] = float(layer)
        is_beam = layer > 0

        dT = thermal_gradient_delta_T(z, is_beam)
        dT_values[idx] = dT
        if dT > 1.0e-12:
            counts["thermal_loaded_cells"] += 1

        if layer == 3:
            counts["top_layer_cells"] += 1
            s0_values[idx] = -4.0e6
            counts["top_prestress_cells"] += 1
        elif layer == 1:
            counts["bottom_layer_cells"] += 1
            s0_values[idx] = -15.0e6
            counts["bottom_prestress_cells"] += 1
        elif layer == 2:
            counts["middle_layer_cells"] += 1
            s0_values[idx] = 0.0

    delta_T.vector().set_local(dT_values)
    delta_T.vector().apply("insert")
    layer_tag.vector().set_local(layer_values)
    layer_tag.vector().apply("insert")
    s0_xx.vector().set_local(s0_values)
    s0_xx.vector().apply("insert")

    counts["max_delta_T_C"] = float(dT_values.max()) if len(dT_values) else 0.0
    return delta_T, layer_tag, s0_xx, counts


def eps(u):
    return sym(grad(u))


def thermal_stress_tensor(delta_T, lmbda, mu):
    """
    Directional-rebar approximation for C:eps_th.
    For a simpler model, remove rho_i*E_s terms but keep this weak-form sign.
    """
    base = 3.0 * lmbda + 2.0 * mu
    sxx_th = (base + rho_x * E_s) * alpha_eff * delta_T
    syy_th = (base + rho_y * E_s) * alpha_eff * delta_T
    szz_th = (base + rho_z * E_s) * alpha_eff * delta_T
    return as_tensor(((sxx_th, 0.0, 0.0),
                      (0.0, syy_th, 0.0),
                      (0.0, 0.0, szz_th)))


def weak_form_snippet(u, v, f_body, sigma_mech, delta_T, s0_xx, dx):
    """
    Use in the final script:
      a = mechanical stiffness
      L = self weight + thermal load - prestress initial stress
    Do not compute -div(sigma_th) for DG0 temperature fields.
    """
    mu = E_c / (2.0 * (1.0 + nu))
    lmbda = E_c * nu / ((1.0 + nu) * (1.0 - 2.0 * nu))
    sigma_th = thermal_stress_tensor(delta_T, lmbda, mu)
    sigma0 = as_tensor(((s0_xx, 0.0, 0.0),
                        (0.0, 0.0, 0.0),
                        (0.0, 0.0, 0.0)))

    a = inner(sigma_mech(u), eps(v)) * dx
    L = dot(f_body, v) * dx + inner(sigma_th, eps(v)) * dx - inner(sigma0, eps(v)) * dx
    return a, L


REQUIRED_CASE3_JSON_FIELDS = [
    "converged",
    "beam_cells",
    "left_pier_cells",
    "right_pier_cells",
    "top_layer_cells",
    "middle_layer_cells",
    "bottom_layer_cells",
    "top_prestress_cells",
    "bottom_prestress_cells",
    "thermal_loaded_cells",
    "max_delta_T_C",
    "mid_span_uz_m",
    "baseline_mid_span_uz_m",
    "thermal_delta_uz_m",
    "thermal_to_baseline_ratio",
    "max_von_mises_pa",
    "output_dir",
    "output_files",
]


