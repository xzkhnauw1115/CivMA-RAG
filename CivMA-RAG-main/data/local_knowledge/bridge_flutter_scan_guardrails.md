# 目标桥梁颤振扫描任务 Scanlan 简化颤振扫描建模规则卡 / reference flutter scan task flutter scan guardrails

Keywords: 颤振扫描任务, flutter_scan, flutter, Scanlan, 颤振, 风速扫描, generalized eigenvalue, SLEPcEigenSolver, aerodynamic stiffness, FEniCS 2019.1.0, 目标桥梁.

This card is mandatory for reference bridge flutter scan task. Also read `bridge_eigen_solver_ladder.md` before writing or repairing flutter scan task scripts. Flutter scan task is a simplified parametric flutter-stability scan using structural eigenvalues and a conservative Scanlan aerodynamic stiffness reduction.

## Core Decisions

1. Use old `dolfin` FEniCS 2019.1.0 APIs and `SLEPcEigenSolver(K, M)`.
2. This is not a fluid-flow simulation and not a transient wind-response simulation. Generate only the requested eigenvalue parameter scan.
3. Assemble a structural stiffness matrix `K_struct` and a density-weighted mass matrix `M`.
4. Do not subtract a Python scalar directly from a sparse matrix. The aerodynamic term must be converted into a bilinear form and assembled as `K_aero_matrix(U)`.
5. If an eigenvalue becomes non-positive, report instability for that wind speed instead of taking `sqrt` of a negative number.

## Material Model

Use the case-6 equivalent stiffness:

- Beam box-girder `alpha_EI` interpolation.
- Directional rebar stiffness as in static prestress task if robust; otherwise use a clearly reported equivalent isotropic beam stiffness for the eigenvalue scan.
- Prestress stress-stiffening is represented by multiplying main-girder stiffness by `1.10`.
- Pier material remains C50.
- Mass matrix uses equivalent beam density from `alpha_EA`, not the stiffness multiplier.

## Scanlan Aerodynamic Stiffness

Given:

- `rho_air = 1.225 kg/m3`
- `B = 12.0 m`
- `L_span = 135.0 m`
- `H4_star = 2.5`
- `U = 20, 25, ..., 120 m/s`

Total simplified stiffness scale:

```python
K_aero_total = 0.5*rho_air*U**2*B*H4_star*(L_span/B)
```

For a 3D continuum mesh without an explicit torsional DOF, use a documented approximation such as vertical deck-top negative stiffness:

```python
k_aero_area = K_aero_total/(L_span*B)
a_aero = k_aero_area*u_trial[2]*v_test[2]*ds_midspan_top
K_total = K_struct.copy()
K_total.axpy(-1.0, assemble(a_aero), True)
```

The top-facet/midspan-facet count must be printed and must be nonzero. If facet marking is unreliable, use a volume-regularized midspan deck region and report the approximation explicitly.


## Boundary Conditions And Eigenproblem Stability

This is the most common failure source for flutter scan task. The zero-wind baseline eigenproblem must be well constrained before any wind scan.

1. Do not use fake DOF counts such as `3*(left_pier_cells+right_pier_cells)`. The script must compute actual constrained DOFs from the generated boundary conditions.
2. Coarse `BoxMesh` grids often have no vertex exactly inside the 4 m pier footprints `[70.5,74.5]` and `[205.5,209.5]`. If `DirichletBC(..., method='pointwise')` is used with those exact x-limits, `base_fixed_dofs` can be zero even when pier cells exist.
3. Use one of these robust approaches:
   - Choose mesh divisions whose x-nodes include the pier footprints, or
   - Use a tolerance tied to mesh size, for example `tol_x = max(0.5*h_x, 2.5)` around pier centerlines `x=72.5` and `x=207.5`, or
   - Mark bottom facets/cells first and then locate DOFs from the marked boundary.
4. After creating BCs, compute actual DOF counts, for example:

```python
base_dofs = set()
end_dofs = set()
for bc in bcs_base:
    base_dofs.update(bc.get_boundary_values().keys())
for bc in bcs_end:
    end_dofs.update(bc.get_boundary_values().keys())
print('Actual base_fixed_dofs:', len(base_dofs))
print('Actual end_vertical_dofs:', len(end_dofs))
if len(base_dofs) == 0 or len(end_dofs) == 0:
    print('CRITICAL ERROR: boundary condition DOF count is zero')
    sys.exit(1)
```

5. Apply the same BCs to both `K_struct` and `M`. For constrained DOFs in generalized eigenproblems, prefer applying BCs consistently and then ignoring trivial constrained modes if they appear.
6. If the baseline solver returns `nconv=0`, first check actual BC DOF counts and matrix diagonal ranges before changing wind/aero parameters. A zero base-fixed DOF count is a modeling error, not a flutter difficulty.
## Eigenvalue Workflow

1. Apply boundary conditions to both stiffness and mass matrices.
2. Solve zero-wind baseline: `K_struct phi = lambda M phi`.
3. Convert positive eigenvalue to frequency: `f = sqrt(lambda)/(2*pi)`.
4. For each wind speed, solve `K_total(U) phi = lambda(U) M phi`.
5. Frequency drop: `df_pct = (f0 - f1)/f0*100`.
6. Critical speed is the first wind speed with `df_pct > 30` or non-positive eigenvalue.


## Solver Ladder Requirement

Coder must follow `bridge_eigen_solver_ladder.md`:

1. First run a zero-wind baseline modal smoke test.
2. Do not start wind scanning until `f0_Hz > 0` and finite.
3. Do not use large 3D meshes for the first eigen attempt. Keep `V.dim() <= 12000` for the smoke test.
4. If 3D SLEPc still returns `nconv == 0`, switch to the documented 1D equivalent modal surrogate and report `model_level` honestly.
5. Long 404 polling without stdout progress should be treated as timeout risk; reduce model level instead of increasing mesh.
## Required Outputs

CSV: `bridge_flutter_scan.csv`

Required columns:

- `U_mps`
- `f1_Hz`
- `df_pct`
- optionally `status`

Required JSON fields:

```json
{
  "converged": true,
  "analysis_type": "scanlan_flutter_eigen_scan",
  "f0_Hz": 0.0,
  "U_cr_mps": null,
  "max_df_pct": 0.0,
  "wind_steps_completed": 21,
  "beam_cells": 1,
  "left_pier_cells": 1,
  "right_pier_cells": 1,
  "aero_facets_or_cells": 1,
  "base_fixed_dofs": 1,
  "end_vertical_dofs": 1,
  "output_files": ["bridge_flutter_scan.csv"]
}
```

## Failure Criteria

Researcher must reject the result if:

- The aerodynamic term is a scalar subtracted from `K_struct` instead of an assembled bilinear matrix.
- The mass matrix omits density.
- Zero-wind frequency `f0_Hz` is missing, non-positive, NaN, or infinite.
- Fewer than 21 wind speeds are scanned.
- Negative eigenvalues are square-rooted as if stable frequencies.
- `aero_facets_or_cells` is zero.
- `base_fixed_dofs` or `end_vertical_dofs` is zero, or these counts are computed from cell counts instead of actual boundary-condition DOFs.
- The CSV is not actually written.





