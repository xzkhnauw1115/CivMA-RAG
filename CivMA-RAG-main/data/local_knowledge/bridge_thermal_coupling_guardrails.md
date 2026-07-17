# 目标桥梁温度梯度任务温度梯度热-力耦合DG0 sigma_th建模规则卡 / reference thermal gradient task thermoelastic guardrails

Keywords: 温度梯度任务, 温度梯度, 热-力耦合, thermoelastic, thermal strain, sigma_th, DG0, 分层等效, 预应力初应力, FEniCS 2019.1.0, 目标桥梁。

This card reduces hallucinations for thermal gradient task. Thermal gradient task is not transient heat transfer. It is a prescribed temperature-field linear static thermoelastic analysis: self weight, smeared reinforcement stiffness, and prestress initial stress from static prestress task, plus thermal strain from the vertical temperature gradient.

## Weak Form

Prefer adding temperature as equivalent initial strain or equivalent thermal stress in the weak form. Do not compute a strong-form body force `f_th = -div(sigma_th)` for DG0 fields.

IMPORTANT conflict resolution: the user prompt may describe `f_th = -div(sigma_th)` as the thermal load. Treat that as continuum-mechanics notation only. In the actual FEniCS 2019 DG0 implementation for this project, the code must use the equivalent weak-form term `+ inner(sigma_th, eps(v))*dx`. A script that literally calls `div(sigma_th)` on DG0/layer fields is considered a likely hallucination and should be rewritten.

Stable pattern:

```python
def eps(u):
    return sym(grad(u))

def sigma_mech(u):
    return lmbda*tr(eps(u))*Identity(3) + 2.0*mu*eps(u) + sigma_rebar_directional(u)

sigma_th = as_tensor(((sxx_th, 0.0, 0.0),
                      (0.0, syy_th, 0.0),
                      (0.0, 0.0, szz_th)))

a = inner(sigma_mech(u), eps(v))*dx
L = dot(f_body, v)*dx + inner(sigma_th, eps(v))*dx - inner(sigma0, eps(v))*dx
```

Notes:
- Linear thermoelastic stress can be written as `sigma = C:eps(u) - C:eps_th + sigma0`.
- After moving known terms to the right side, the thermal term is `+ inner(C:eps_th, eps(v))*dx`.
- Keep the project initial-stress convention `L = body - inner(sigma0, eps(v))*dx`; do not flip the prestress sign repeatedly after failures.
- `sigma0` and `sigma_th` must be second-order tensors, not Voigt vectors.

## Avoid Strong-Form div on DG0 Thermal Stress

The user prompt's `f_th = -div(sigma_th)` is a conceptual description. In FEniCS 2019, if `sigma_th` comes from DG0 temperature or DG0 layer fields, direct `div(sigma_th)` is fragile:
- DG0 is discontinuous between cells, so strong divergence is not a stable representation of layer-interface thermal bending.
- `div()` on scalar or wrong-shaped tensors causes UFL shape errors.
- The thermal effect can disappear or become mesh-noise dominated.

Use the weak form instead:

```python
L_thermal = inner(sigma_th, eps(v))*dx
```

If a material marker `dx(1)` is reliable, use `dx(1)` for the main beam. Otherwise set `sigma_th = 0` outside the beam through DG0 masks.

## Temperature and Layers

Temperature is applied only in the main beam:

```python
dT = 15.0 * exp(-2.0*(131.0 - z))
T = 20.0 + dT
```

Pier cells use the reference temperature, so `dT = 0`. Beam layers must be classified from the cell midpoint and dynamic bottom elevation:
- Top layer: `129.0 <= z <= 131.0`, top prestress about `-4.0 MPa`.
- Bottom layer: `z_bottom(x) <= z <= z_bottom(x) + 2.0`, bottom prestress about `-15.0 MPa`.
- Middle layer: `z_bottom(x)+2.0 < z < 129.0`, no longitudinal prestress initial stress.

Never hard-code the bottom layer as `118~120`. The bridge bottom elevation varies with `x`.

## Thermal Expansion with Directional Rebar

For pure isotropic 3D concrete:

```python
beta = E / (1.0 - 2.0*nu) * alpha
sigma_th = beta * dT * Identity(3)
```

With case-2 directional smeared rebar stiffness, a stable approximation is:

```python
sxx_th = ((3*lmbda + 2*mu) + rho_x*E_s) * alpha_eff_x * dT
syy_th = ((3*lmbda + 2*mu) + rho_y*E_s) * alpha_eff_y * dT
szz_th = ((3*lmbda + 2*mu) + rho_z*E_s) * alpha_eff_z * dT
```

A simpler script may use one `alpha_eff = 1.02e-5`, but it must report the chosen thermal model in JSON.

## FEniCS 2019 Stable Implementation

Prefer DG0 arrays filled by cell midpoint for temperature, layer tags, prestress, and material fields:

```python
DG0 = FunctionSpace(mesh, "DG", 0)
delta_T = Function(DG0)
layer_tag = Function(DG0)
s0 = Function(DG0)

dT_values = delta_T.vector().get_local()
layer_values = layer_tag.vector().get_local()
s0_values = s0.vector().get_local()

for cell in cells(mesh):
    mp = cell.midpoint()
    x, y, z = mp.x(), mp.y(), mp.z()
    # classify and assign by cell.index()

delta_T.vector().set_local(dT_values)
delta_T.vector().apply("insert")
```

`UserExpression.eval()` is also acceptable, but it must use `math.exp` or imported `exp`, assign `value[0]`, and set `degree`.

## Required Anti-Hallucination Statistics

Thermal gradient task JSON must include at least:

```json
{
  "converged": true,
  "beam_cells": 1,
  "left_pier_cells": 1,
  "right_pier_cells": 1,
  "top_layer_cells": 1,
  "middle_layer_cells": 1,
  "bottom_layer_cells": 1,
  "top_prestress_cells": 1,
  "bottom_prestress_cells": 1,
  "thermal_loaded_cells": 1,
  "max_delta_T_C": 15.0,
  "mid_span_uz_m": 0.0,
  "baseline_mid_span_uz_m": 0.0,
  "thermal_delta_uz_m": 0.0,
  "thermal_to_baseline_ratio": 0.0,
  "max_von_mises_pa": 0.0,
  "output_dir": "...",
  "output_files": ["bridge_thermal_gradient_disp.pvd", "bridge_thermal_gradient_thermal.pvd", "bridge_thermal_gradient_vonmises.pvd"]
}
```

If `top_layer_cells == 0`, `bottom_layer_cells == 0`, `thermal_loaded_cells == 0`, or `max_delta_T_C < 10`, the model is invalid and must not be accepted.

## Physics Checks

- The thermal gradient must change the midspan displacement relative to the no-temperature baseline. If `abs(thermal_delta_uz_m) < 1e-8`, the thermal term likely did not enter the weak form.
- The user expects the gradient to produce upward camber. If the result is opposite, first check the temperature field, thermal weak-form sign, and boundary conditions. Do not blindly flip signs.
- `max_von_mises_pa` must be computed on structural material cells, not void or ghost cells.
- `.pvd` and CSV/JSON outputs must be really written to the output directory.


