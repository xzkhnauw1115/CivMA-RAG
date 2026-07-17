"""
FEniCS 2019.1.0 golden recipe for reference bridge P-Delta task.

Purpose:
- Stable snippets for geometric nonlinear P-Delta analysis.
- Use Total Lagrangian St. Venant-Kirchhoff with derivative-generated tangent.
- Compare nonlinear and linear displacement on the same mesh.
- Use equivalent external prestress loads, not sigma0 initial stress.

This is a recipe, not a full final bridge model. Coder must adapt these
patterns inside temp_scripts/fenics_drafts/current_fenics_script.py.
"""

from dolfin import *
import math

E_c = 3.45e10
nu = 0.20
rho_c = 2550.0
g = 9.81
E_s = 2.0e11
rho_x = 0.0102
rho_y = 0.0015
rho_z = 0.0012
w_net = 2.109e6  # N/m, upward equivalent tendon load
Z_TOP = 131.0


def z_bottom_bridge(x):
    if x <= 72.5:
        return 126.5 - 6.5*(1.0 - x/72.5)**2
    if x <= 207.5:
        return 120.0 + 3.0*(2.0*abs(x - 140.0)/135.0)**2
    xr = 280.0 - x
    return 126.5 - 6.5*(1.0 - xr/72.5)**2


def beam_height_bridge(x):
    return max(1.0, Z_TOP - z_bottom_bridge(x))


def is_left_pier_cell(x, y, z):
    return 70.5 <= x <= 74.5 and -6.0 <= y <= 6.0 and 0.0 <= z <= 120.0


def is_right_pier_cell(x, y, z):
    return 205.5 <= x <= 209.5 and -6.0 <= y <= 6.0 and 0.0 <= z <= 120.0


def is_beam_cell(x, y, z):
    return 0.0 <= x <= 280.0 and -6.0 <= y <= 6.0 and z_bottom_bridge(x) <= z <= Z_TOP


def fill_pdelta_nonlinear_dg0_fields(mesh, DG0):
    beam_mask = Function(DG0)
    pier_mask = Function(DG0)
    rho_func = Function(DG0)
    lambda_func = Function(DG0)
    mu_func = Function(DG0)
    prestress_bz = Function(DG0)

    beam_vals = beam_mask.vector().get_local()
    pier_vals = pier_mask.vector().get_local()
    rho_vals = rho_func.vector().get_local()
    lam_vals = lambda_func.vector().get_local()
    mu_vals = mu_func.vector().get_local()
    pz_vals = prestress_bz.vector().get_local()

    E_void = E_c*1.0e-8
    mu_void = E_void/(2.0*(1.0 + nu))
    lam_void = E_void*nu/((1.0 + nu)*(1.0 - 2.0*nu))
    mu_c = E_c/(2.0*(1.0 + nu))
    lam_c = E_c*nu/((1.0 + nu)*(1.0 - 2.0*nu))

    counts = {
        "beam_cells": 0,
        "left_pier_cells": 0,
        "right_pier_cells": 0,
        "void_cells": 0,
        "prestress_body_force_cells": 0,
    }

    for cell in cells(mesh):
        cid = cell.index()
        mp = cell.midpoint()
        x, y, z = mp.x(), mp.y(), mp.z()
        in_left = is_left_pier_cell(x, y, z)
        in_right = is_right_pier_cell(x, y, z)
        in_beam = is_beam_cell(x, y, z)

        if in_beam:
            beam_vals[cid] = 1.0
            rho_vals[cid] = 2550.0*0.18  # case-specific code should interpolate alpha_EA
            lam_vals[cid] = lam_c*0.80   # case-specific code should interpolate alpha_EI
            mu_vals[cid] = mu_c*0.80
            area_equiv = 12.0*beam_height_bridge(x)
            pz_vals[cid] = w_net/area_equiv
            counts["beam_cells"] += 1
            counts["prestress_body_force_cells"] += 1
        elif in_left or in_right:
            pier_vals[cid] = 1.0
            rho_vals[cid] = rho_c
            lam_vals[cid] = lam_c
            mu_vals[cid] = mu_c
            if in_left:
                counts["left_pier_cells"] += 1
            else:
                counts["right_pier_cells"] += 1
        else:
            rho_vals[cid] = 0.0
            lam_vals[cid] = lam_void
            mu_vals[cid] = mu_void
            counts["void_cells"] += 1

    for fun, vals in [
        (beam_mask, beam_vals), (pier_mask, pier_vals), (rho_func, rho_vals),
        (lambda_func, lam_vals), (mu_func, mu_vals), (prestress_bz, pz_vals)
    ]:
        fun.vector().set_local(vals)
        fun.vector().apply("insert")

    counts["max_prestress_body_force_z"] = float(pz_vals.max()) if len(pz_vals) else 0.0
    return beam_mask, pier_mask, rho_func, lambda_func, mu_func, prestress_bz, counts


def eps_small(u):
    return sym(grad(u))


def sigma_linear(u, lmbda, mu, beam_mask):
    e = eps_small(u)
    ex = as_vector((1.0, 0.0, 0.0))
    ey = as_vector((0.0, 1.0, 0.0))
    ez = as_vector((0.0, 0.0, 1.0))
    sig = lmbda*tr(e)*Identity(3) + 2.0*mu*e
    sig += beam_mask*rho_x*E_s*e[0, 0]*outer(ex, ex)
    sig += beam_mask*rho_y*E_s*e[1, 1]*outer(ey, ey)
    sig += beam_mask*rho_z*E_s*e[2, 2]*outer(ez, ez)
    return sig


def nonlinear_residual_and_jacobian(u, du, v, lmbda, mu, beam_mask, body_force):
    d = u.geometric_dimension()
    I = Identity(d)
    F_def = variable(I + grad(u))
    C = F_def.T*F_def
    E_GL = 0.5*(C - I)

    psi = 0.5*lmbda*tr(E_GL)**2 + mu*inner(E_GL, E_GL)
    psi += beam_mask*0.5*rho_x*E_s*E_GL[0, 0]**2
    psi += beam_mask*0.5*rho_y*E_s*E_GL[1, 1]**2
    psi += beam_mask*0.5*rho_z*E_s*E_GL[2, 2]**2

    Pi = psi*dx - dot(body_force, u)*dx
    R = derivative(Pi, u, v)
    J = derivative(R, u, du)
    return R, J


def configure_newton_solver(problem):
    solver = NonlinearVariationalSolver(problem)
    prm = solver.parameters["newton_solver"]
    prm["absolute_tolerance"] = 1.0e-8
    prm["relative_tolerance"] = 1.0e-7
    prm["maximum_iterations"] = 50
    prm["relaxation_parameter"] = 0.7
    return solver


REQUIRED_CASE4_JSON_FIELDS = [
    "converged",
    "nonlinear_converged",
    "linear_converged",
    "newton_total_iterations",
    "load_steps_completed",
    "beam_cells",
    "left_pier_cells",
    "right_pier_cells",
    "void_cells",
    "base_fixed_dofs",
    "end_vertical_dofs",
    "prestress_body_force_cells",
    "max_prestress_body_force_z",
    "pier_top_ux_nonlinear_m",
    "pier_top_ux_linear_m",
    "p_delta_amplification_factor",
    "output_files",
]


