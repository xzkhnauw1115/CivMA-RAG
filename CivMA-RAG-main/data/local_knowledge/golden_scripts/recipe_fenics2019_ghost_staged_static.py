"""
Golden pattern: FEniCS 2019 staged construction with ghost elements.
Keywords: ghost element, staged construction, DG0 material field, independent static stages,
BoxMesh, linear elasticity, zero density for inactive cells, CSV/PVD/JSON output.
"""
from dolfin import *
import json
import csv
import os

set_log_level(LogLevel.ERROR)

E_C = 3.45e10
NU = 0.20
RHO_C = 2550.0
G = 9.81
E_GHOST = 1.0


def lame_from_E(E):
    mu = E / (2.0 * (1.0 + NU))
    lmbda = E * NU / ((1.0 + NU) * (1.0 - 2.0 * NU))
    return lmbda, mu


def sigma_eps(u, E):
    lmbda, mu = lame_from_E(E)
    eps = sym(grad(u))
    return lmbda * tr(eps) * Identity(3) + 2.0 * mu * eps


def bottom_z(x):
    if x < 72.5:
        return 126.5 - 6.5 * (1.0 - x / 72.5) ** 2
    if x <= 207.5:
        return 120.0 + 3.0 * (2.0 * abs(x - 140.0) / 135.0) ** 2
    return 126.5 - 6.5 * (1.0 - (280.0 - x) / 72.5) ** 2


def height_at(x):
    return 131.0 - bottom_z(x)


def alpha_ei(x):
    h = min(max(height_at(x), 8.0), 11.0)
    t = (h - 8.0) / 3.0
    return 0.712 + t * (0.872 - 0.712)


def alpha_ea(x):
    h = min(max(height_at(x), 8.0), 11.0)
    t = (h - 8.0) / 3.0
    return 0.135 + t * (0.229 - 0.135)


def is_pier(x):
    return (70.5 <= x[0] <= 74.5 or 205.5 <= x[0] <= 209.5) and -6.0 <= x[1] <= 6.0 and 0.0 <= x[2] <= 120.0


def is_girder(x):
    return 0.0 <= x[0] <= 280.0 and -6.0 <= x[1] <= 6.0 and bottom_z(x[0]) <= x[2] <= 131.0


def is_stage1_segment(x):
    # Follow the task statement exactly; do not silently move this segment to pier locations.
    return 120.0 <= x[0] <= 160.0 and is_girder(x)


def active_for_stage(stage, x):
    if is_pier(x):
        return True
    if stage == 0:
        return False
    if stage == 1:
        return is_stage1_segment(x)
    return is_girder(x)


def fill_stage_fields(mesh, stage, E_func, rho_func):
    E_values = E_func.vector().get_local()
    rho_values = rho_func.vector().get_local()
    dofmap = E_func.function_space().dofmap()
    active = 0
    ghost = 0
    loaded = 0
    rebar_factor = 1.0 + 0.0206 * 2.0e11 / E_C
    for cell in cells(mesh):
        mp = cell.midpoint()
        x = (mp.x(), mp.y(), mp.z())
        dof = dofmap.cell_dofs(cell.index())[0]
        if active_for_stage(stage, x):
            active += 1
            if is_pier(x):
                E_values[dof] = E_C
                rho_values[dof] = RHO_C
            elif is_girder(x):
                E_values[dof] = E_C * alpha_ei(x[0]) * rebar_factor
                rho_values[dof] = RHO_C * alpha_ea(x[0])
            else:
                E_values[dof] = E_GHOST
                rho_values[dof] = 0.0
        else:
            ghost += 1
            E_values[dof] = E_GHOST
            rho_values[dof] = 0.0
        if rho_values[dof] > 0.0:
            loaded += 1
    E_func.vector().set_local(E_values)
    E_func.vector().apply("insert")
    rho_func.vector().set_local(rho_values)
    rho_func.vector().apply("insert")
    return active, ghost, loaded


class PierBase(SubDomain):
    def inside(self, x, on_boundary):
        return on_boundary and near(x[2], 0.0, 1e-6) and (
            70.5 - 1e-6 <= x[0] <= 74.5 + 1e-6 or 205.5 - 1e-6 <= x[0] <= 209.5 + 1e-6
        ) and -6.0 - 1e-6 <= x[1] <= 6.0 + 1e-6


class EndVertical(SubDomain):
    def inside(self, x, on_boundary):
        return on_boundary and (near(x[0], 0.0, 1e-6) or near(x[0], 280.0, 1e-6)) and x[2] >= 120.0 - 1e-6


def solve_stage(mesh, V, stage, out_dir):
    DG0 = FunctionSpace(mesh, "DG", 0)
    E = Function(DG0)
    rho = Function(DG0)
    active, ghost, loaded = fill_stage_fields(mesh, stage, E, rho)
    print("stage", stage, "active_cells", active, "ghost_cells", ghost, "loaded_cells", loaded)
    if active == 0 or loaded == 0:
        raise RuntimeError("CRITICAL ERROR: no active/loaded cells in stage %d" % stage)

    u = TrialFunction(V)
    v = TestFunction(V)
    a = inner(sigma_eps(u, E), sym(grad(v))) * dx
    L = dot(as_vector((0.0, 0.0, -G * rho)), v) * dx

    bc_base = DirichletBC(V, Constant((0.0, 0.0, 0.0)), PierBase())
    bc_end = DirichletBC(V.sub(2), Constant(0.0), EndVertical())
    bcs = [bc_base, bc_end]
    base_dofs = len(bc_base.get_boundary_values())
    end_dofs = len(bc_end.get_boundary_values())
    print("stage", stage, "base_dofs", base_dofs, "end_vertical_dofs", end_dofs)
    if base_dofs == 0:
        raise RuntimeError("CRITICAL ERROR: pier base BC has zero dofs")

    A = assemble(a, keep_diagonal=True)
    b = assemble(L)
    for bc in bcs:
        bc.apply(A, b)
    A.ident_zeros()
    uh = Function(V)
    solver = LUSolver(A, "default")
    solver.solve(uh.vector(), b)

    mid_uz = float(uh(Point(140.0, 0.0, 131.0))[2])
    if stage == 2:
        File(os.path.join(out_dir, "bridge_staged_construction_disp.pvd")) << uh
    return uh, mid_uz, {"active": active, "ghost": ghost, "loaded": loaded, "base_dofs": base_dofs, "end_dofs": end_dofs}


def main():
    out_dir = os.getcwd()
    mesh = BoxMesh(Point(0.0, -6.0, 0.0), Point(280.0, 6.0, 131.0), 28, 4, 14)
    V = VectorFunctionSpace(mesh, "Lagrange", 1)
    rows = []
    diagnostics = {}
    descriptions = {
        0: "piers active, girder ghost",
        1: "piers plus specified x[120,160] girder segment active",
        2: "completed bridge with equivalent girder material",
    }
    for stage in [0, 1, 2]:
        uh, uz, diag = solve_stage(mesh, V, stage, out_dir)
        rows.append([stage, uz, descriptions[stage]])
        diagnostics[str(stage)] = diag

    csv_path = os.path.join(out_dir, "bridge_staged_construction_stages.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["stage", "u_z_m", "description"])
        writer.writerows(rows)

    print("--- FENICS JOB RESULT ---")
    print(json.dumps({
        "converged": True,
        "stage_mid_span_uz_m": {str(r[0]): r[1] for r in rows},
        "stage_diagnostics": diagnostics,
        "output_files": ["bridge_staged_construction_stages.csv", "bridge_staged_construction_disp.pvd"],
    }, ensure_ascii=True))


if __name__ == "__main__":
    main()


