# 目标桥梁移动荷载动力任务移动列车荷载动力响应建模规则卡 / reference moving-load dynamics task Newmark dynamics guardrails

Keywords: 移动荷载动力任务, moving_load_dynamics, moving train load, Newmark, Newmark-beta, 隐式动力学, 移动荷载, 动静位移比, FEniCS 2019.1.0, 目标桥梁.

This card is mandatory for reference bridge moving-load dynamics task. Moving-load dynamics task is an implicit linear elastodynamic time-history analysis. The script must first solve the gravity static state, then run Newmark-beta time integration under a moving top-surface Gaussian load.

## Core Decisions

1. Use old `dolfin` FEniCS 2019.1.0 APIs only. Do not use DOLFINx, `LinearProblem`, or PETSc-only wrappers.
2. Use a linear elastic model. Do not switch to nonlinear geometry or modal-only analysis.
3. The mass matrix must include density: `M = assemble(rho_func*inner(u_trial, v_test)*dx)`. Never use `assemble(inner(u, v)*dx)` without `rho`.
4. Self-weight is the static base load. Solve `K u_static = F_body` before the dynamic loop. Dynamic displacement history may store total displacement or dynamic increment, but the JSON/CSV must state which one is written.
5. Newmark parameters are fixed: `beta = 0.25`, `gamma = 0.5`, `dt = 0.1`, `T = 4.0`, 40 steps.

## Geometry And Materials

Reuse the reference bridge geometry and DG0 material-field pattern from the stable static prestress task recipes:

- Beam cells: `z_bottom_bridge(x) <= z <= 131`.
- Pier cells: left and right pier boxes, `0 <= z <= 120`.
- Void cells in a bounding-box mesh: tiny ghost stiffness and zero density.
- Beam density uses box-girder equivalent `rho_eff` from `alpha_EA`; pier density is 2550 kg/m3.
- Beam modulus uses box-girder `alpha_EI`, isotropic rebar smearing, and the case-5 prestress stiffness multiplier `1.06`.

For automated tests, use a coarse mesh but print both production target and actual mesh. Required cell counts must be greater than zero.

## Moving Load Implementation

The user describes a top-surface Gaussian force:

`f_dyn(x,t) = Amp * exp(-(x - v_train*t)^2 / 10.0)` with `Amp = 100000 N`, `v_train = 80 m/s`, acting near `z = 131` and `y in [-2, 2]`.

Preferred robust implementation in FEniCS 2019:

1. Mark top deck facets where `near(z, 131)` and `-2 <= y <= 2` and beam exists.
2. Use `ds_top = Measure('ds', domain=mesh, subdomain_data=facet_markers)`.
3. Use a `UserExpression` or `Expression` with mutable time parameter `t`.
4. Apply it as vertical traction: `L_dyn = -p_dyn*v_test[2]*ds_top(TOP_LOAD_MARK)`.

If top-facet marking is unreliable on a coarse bounding-box mesh, use a volume-regularized load over a thin top layer `129 <= z <= 131`, and explicitly report `load_model = "volume_regularized_top_layer"`. Do not silently treat the 100 kN load as a full-volume force density.

## Newmark Formulas Without Damping

Let `u_n`, `v_n`, `a_n` be displacement, velocity, acceleration vectors. For zero damping:

```python
a0 = 1.0/(beta*dt*dt)
a2 = 1.0/(beta*dt)
a3 = 1.0/(2.0*beta) - 1.0
K_eff = K + a0*M
F_eff = F_total + M*(a0*u_n + a2*v_n + a3*a_n)
```

After solving `u_np1`:

```python
a_np1 = a0*(u_np1 - u_n) - a2*v_n - a3*a_n
v_np1 = v_n + dt*((1.0 - gamma)*a_n + gamma*a_np1)
```

Apply the same displacement boundary conditions to `K`, `M`, `K_eff`, and every RHS vector.

## Required Outputs

CSV: `bridge_moving_load_dynamics_dynamic.csv`

Required columns:

- `time_s`
- `midspan_uz_m`
- optionally `load_center_x_m`

Required JSON fields after `--- FENICS JOB RESULT ---`:

```json
{
  "converged": true,
  "analysis_type": "implicit_newmark_dynamics",
  "time_steps_completed": 40,
  "dt_s": 0.1,
  "u_z_static_m": 0.0,
  "u_z_max_m": 0.0,
  "t_max_s": 0.0,
  "dynamic_static_ratio": 0.0,
  "beam_cells": 1,
  "left_pier_cells": 1,
  "right_pier_cells": 1,
  "top_load_facets_or_cells": 1,
  "base_fixed_dofs": 1,
  "end_vertical_dofs": 1,
  "output_files": ["bridge_moving_load_dynamics_dynamic.csv"]
}
```

## Failure Criteria

Researcher must reject the result if:

- The script does not solve the self-weight static state before the dynamic loop.
- `M` does not include density.
- Fewer than 40 Newmark steps complete.
- Moving-load support facets/cells count is zero.
- Required boundary-condition DOF counts are zero.
- `dynamic_static_ratio` is missing, NaN, infinite, or computed with a near-zero static denominator without a guard.
- The CSV is not actually written.


