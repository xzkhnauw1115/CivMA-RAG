# 目标桥梁P-Delta任务高墩几何非线性 P-Delta 建模规则卡 / reference P-Delta task P-Delta guardrails

Keywords: P-Delta任务, P-Delta, P Delta, 几何非线性, geometric nonlinear, St Venant Kirchhoff, Total Lagrangian, NonlinearVariationalSolver, load stepping, 等效荷载法, 目标桥梁, FEniCS 2019.1.0.

This card is mandatory for reference bridge P-Delta task. P-Delta task is a static geometric-nonlinear comparison: solve the same coarse bridge model with a linear small-strain formulation and with a total-Lagrangian St. Venant-Kirchhoff formulation, then report the P-Delta amplification factor.

## Core Decisions

1. Do not use the prestress initial-stress field method for P-Delta task. The user explicitly selected the equivalent-load method because prestress should remain an external load in the geometric-nonlinear solve.
2. Do not use DOLFINx APIs, `LinearProblem`, or PETSc-only code. Use old `dolfin` FEniCS 2019.1.0 APIs.
3. If using a full bounding-box `BoxMesh`, material marker 0 cannot have exactly zero stiffness in the global nonlinear problem. Use a tiny ghost stiffness such as `E_void = E_c*1e-8` and zero density, and exclude void cells from engineering result statistics.
4. For automated testing, coarse mesh is acceptable, but the script must print the production mesh target and the actual coarse mesh used.

## Geometry And Cell Classification

Use the user-provided bridge coordinates:

- Total length: `x in [0, 280]`
- Width: `y in [-6, 6]`
- Top elevation: `z_top = 131`
- Left pier: `70.5 <= x <= 74.5`, `-6 <= y <= 6`, `0 <= z <= 120`
- Right pier: `205.5 <= x <= 209.5`, `-6 <= y <= 6`, `0 <= z <= 120`
- Beam cells: `z_bottom_bridge(x) <= z <= 131`

Dynamic bottom elevation:

```python
def z_bottom_bridge(x):
    if x <= 72.5:
        return 126.5 - 6.5*(1.0 - x/72.5)**2
    if x <= 207.5:
        return 120.0 + 3.0*(2.0*abs(x - 140.0)/135.0)**2
    xr = 280.0 - x
    return 126.5 - 6.5*(1.0 - xr/72.5)**2
```

The beam layer logic from cases 2 and 3 may be reused for equivalent material properties, but P-Delta task does not apply prestress as `sigma0`.

## Material Fields

Use DG0 fields filled by `cell.index()`:

- `beam_mask`: 1 for beam, 0 otherwise.
- `pier_mask`: 1 for pier cells, 0 otherwise.
- `rho_func`: effective beam density in beam cells, C50 density in pier cells, 0 in void cells.
- `lmbda_func`, `mu_func`: beam/pier/ghost elastic constants.
- `prestress_body_z`: upward equivalent prestress body-force density in beam cells.

For beam cells, include stiffness reduction and directional rebar as a stable approximation. If full orthotropy is too fragile in nonlinear form, use isotropic reduced concrete in `psi` plus rebar energy terms:

```python
psi = 0.5*lmbda_func*tr(E_GL)**2 + mu_func*inner(E_GL, E_GL)
psi += beam_mask*0.5*rho_x*E_s*E_GL[0,0]**2
psi += beam_mask*0.5*rho_y*E_s*E_GL[1,1]**2
psi += beam_mask*0.5*rho_z*E_s*E_GL[2,2]**2
```

This energy-based form lets FEniCS derive a consistent tangent by `derivative`.

## Equivalent Prestress Loads

P-Delta task uses equivalent external loads, not initial stress:

1. Upward parabolic tendon resultant:
   - `w_net = 2.109e6 N/m` upward.
   - Convert line load to volume body force in beam cells: `q_prestress_z = w_net / A_equiv(x)`.
   - A stable approximation is `A_equiv(x) = 12.0 * beam_height(x)`, with `beam_height = 131.0 - z_bottom_bridge(x)`.
   - Add to body force: `f_z = -rho_func*g + beam_mask*q_prestress_z*load_scale`.

2. End eccentric moment:
   - `M_total = -4.8e9 N*m`.
   - A robust automated script may approximate this as balanced end-face traction or report `end_moment_model = "omitted_or_simplified"` if traction marking is unreliable.
   - If implemented, use `MeshFunction("size_t", mesh, 2)` and `ds(subdomain_data=facets)`, and print nonzero left/right end facet counts. If facet counts are zero, abort before solving.

Never apply `w_net` directly as `N/m` in a volume integral; it must be converted to `N/m^3` or applied as a boundary/surface traction with correct units.

## Nonlinear Formulation

Use total-Lagrangian St. Venant-Kirchhoff:

```python
u = Function(V)
du = TrialFunction(V)
v = TestFunction(V)
d = mesh.geometry().dim()
I = Identity(d)
F_def = variable(I + grad(u))
C = F_def.T*F_def
E_GL = 0.5*(C - I)

psi = 0.5*lmbda_func*tr(E_GL)**2 + mu_func*inner(E_GL, E_GL)
Pi = psi*dx - dot(f_body, u)*dx
R = derivative(Pi, u, v)
J = derivative(R, u, du)
problem = NonlinearVariationalProblem(R, u, bcs, J)
solver = NonlinearVariationalSolver(problem)
```

Load stepping is mandatory for robustness:

```python
load_scale = Constant(0.0)
for step in range(1, nsteps + 1):
    load_scale.assign(float(step)/nsteps)
    solver.solve()
```

Do not reinitialize `u` between load steps.

## Linear Comparison

Solve a small-strain linear problem on the same mesh, with the same final body loads:

```python
eps = sym(grad(u_trial))
sigma = lmbda_func*tr(eps)*Identity(3) + 2.0*mu_func*eps + beam rebar terms
a_lin = inner(sigma, sym(grad(v)))*dx
L_lin = dot(f_body_final, v)*dx
```

Use the same boundary conditions as the nonlinear solve. P-Delta amplification should be based on a meaningful high-pier horizontal displacement, for example the average or point-probed `u_x` near `(x=72.5, y=0, z=120)` and `(x=207.5, y=0, z=120)`.

If `abs(u_x_lin) < 1e-10`, report the factor as `null` or a guarded large value, not a misleading finite ratio.

## Boundary Conditions

Must include:

- Pier base full fixity: `z=0` and x within pier footprints, `u=(0,0,0)`.
- Beam end vertical supports: `x=0 or x=280`, `z >= z_bottom_bridge(x)`, only `u_z=0`.

Print DOF counts for each boundary condition. Abort if any required BC has zero DOFs.

## Required JSON Fields

P-Delta task output after `--- FENICS JOB RESULT ---` must include at least:

```json
{
  "converged": true,
  "nonlinear_converged": true,
  "linear_converged": true,
  "newton_total_iterations": 1,
  "load_steps_completed": 1,
  "beam_cells": 1,
  "left_pier_cells": 1,
  "right_pier_cells": 1,
  "void_cells": 1,
  "base_fixed_dofs": 1,
  "end_vertical_dofs": 1,
  "prestress_body_force_cells": 1,
  "max_prestress_body_force_z": 1.0,
  "pier_top_ux_nonlinear_m": 0.0,
  "pier_top_ux_linear_m": 0.0,
  "p_delta_amplification_factor": 1.0,
  "output_files": [
    "bridge_pdelta_nonlinear_nonlinear_disp.pvd",
    "bridge_pdelta_nonlinear_linear_disp.pvd"
  ]
}
```

## Failure Criteria

Researcher must reject the result if:

- The nonlinear solve did not complete all load steps.
- Required BC DOF counts are zero.
- Beam or either pier has zero cells.
- Prestress equivalent load is missing or has wrong sign.
- `p_delta_amplification_factor` is reported without a valid linear denominator.
- Displacements are `nan`, `inf`, or absurdly large for a coarse bridge model.
- The script only solves the nonlinear case and omits the required linear comparison.


