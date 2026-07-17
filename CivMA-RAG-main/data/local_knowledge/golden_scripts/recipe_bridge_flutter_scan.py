"""
FEniCS 2019.1.0 golden recipe for reference bridge flutter scan task.

Purpose:
- Read bridge_eigen_solver_ladder.md first; use the solver ladder before full wind scan.
- Stable SLEPc generalized eigenvalue scan pattern.
- Aerodynamic stiffness is assembled as a bilinear matrix, never subtracted as a scalar.
- This is a recipe, not a complete bridge model.
"""

from dolfin import *
import math

RHO_AIR = 1.225
B_DECK = 12.0
L_SPAN = 135.0
H4_STAR = 2.5
WIND_SPEEDS = list(range(20, 121, 5))


def scanlan_total_stiffness(U):
    return 0.5*RHO_AIR*U*U*B_DECK*H4_STAR*(L_SPAN/B_DECK)


def scanlan_area_stiffness(U):
    """Distribute the simplified total aero stiffness over the top area L_span*B."""
    return scanlan_total_stiffness(U)/(L_SPAN*B_DECK)




def collect_bc_dofs(bcs):
    """Return actual constrained DOF ids. Never estimate BC counts from cell counts."""
    dofs = set()
    for bc in bcs:
        dofs.update(int(k) for k in bc.get_boundary_values().keys())
    return dofs


def pier_base_inside_with_mesh_tolerance(x, pier_center, hx):
    """Coarse BoxMesh helper: use tolerance wide enough to catch support nodes."""
    tol_x = max(0.5*float(hx), 2.5)
    return abs(x[0] - float(pier_center)) <= tol_x and -6.0 <= x[1] <= 6.0 and near(x[2], 0.0)


def assert_eigen_bc_counts(base_dofs, end_dofs):
    if len(base_dofs) == 0 or len(end_dofs) == 0:
        raise RuntimeError(
            "FlutterScan eigenproblem has zero BC DOFs: base_fixed_dofs=%d, end_vertical_dofs=%d" %
            (len(base_dofs), len(end_dofs))
        )
def first_positive_frequency(K, M, n_modes=10, tol=1.0e-8):
    eigensolver = SLEPcEigenSolver(K, M)
    eigensolver.parameters["spectrum"] = "smallest real"
    eigensolver.parameters["tolerance"] = tol
    eigensolver.parameters["maximum_iterations"] = 1000
    try:
        eigensolver.parameters["spectral_transform"] = "shift-and-invert"
        eigensolver.parameters["spectral_shift"] = 1.0e-6
    except Exception:
        pass
    eigensolver.solve(n_modes)
    nconv = eigensolver.get_number_converged()
    for i in range(nconv):
        r, c, rx, cx = eigensolver.get_eigenpair(i)
        if r > 0.0:
            return math.sqrt(r)/(2.0*math.pi), r, i, nconv
    return None, None, None, nconv


def build_total_flutter_matrix(K_struct, K_aero, bcs):
    K_total = K_struct.copy()
    K_total.axpy(-1.0, K_aero, True)
    for bc in bcs:
        bc.apply(K_total)
    return K_total


def classify_frequency_drop(f0, f1):
    if f0 is None or f0 <= 0.0:
        return None
    if f1 is None or f1 <= 0.0:
        return 100.0
    return (f0 - f1)/f0*100.0


# Example aero form in a final script:
# u_trial = TrialFunction(V); v_test = TestFunction(V)
# k_area = Constant(scanlan_area_stiffness(U))
# a_aero = k_area*u_trial[2]*v_test[2]*ds_midspan_top(TOP_MARK)
# K_aero = assemble(a_aero)

REQUIRED_CASE6_JSON_FIELDS = [
    "converged",
    "analysis_type",
    "f0_Hz",
    "U_cr_mps",
    "max_df_pct",
    "wind_steps_completed",
    "beam_cells",
    "left_pier_cells",
    "right_pier_cells",
    "aero_facets_or_cells",
    "base_fixed_dofs",
    "end_vertical_dofs",
    "output_files",
]






