"""
FEniCS 2019.1.0 golden recipe: 3D linear elasticity with DG0 body force
and DG0 initial stress.

Purpose:
- Provide a compact, stable old-dolfin template for Coder.
- Avoid DOLFINx APIs.
- Demonstrate DG0 cell-wise values, explicit assembly, pvd/csv output,
  point probing, and standard JSON result marker.

This is a small generic template, not a bridge-specific manual.
"""
from dolfin import *
import os
import csv
import json
import math

output_dir = os.getcwd()
os.makedirs(output_dir, exist_ok=True)

# Geometry and mesh: intentionally small for automated testing.
L, W, H = 10.0, 1.0, 1.0
mesh = BoxMesh(Point(0.0, -W/2.0, 0.0), Point(L, W/2.0, H), 20, 4, 4)

V = VectorFunctionSpace(mesh, "CG", 1)
DG0 = FunctionSpace(mesh, "DG", 0)

E = 3.0e10
nu = 0.20
rho = 2500.0
g = 9.81
mu = E / (2.0*(1.0 + nu))
lmbda = E*nu / ((1.0 + nu)*(1.0 - 2.0*nu))

def eps(u):
    return sym(grad(u))

def sigma_elastic(u):
    return lmbda*tr(eps(u))*Identity(3) + 2.0*mu*eps(u)

# DG0 density and initial stress fields.
rho_fun = Function(DG0)
s0_fun = Function(DG0)
rho_vals = rho_fun.vector().get_local()
s0_vals = s0_fun.vector().get_local()

left_cells = 0
right_cells = 0
initial_stress_cells = 0
for cell in cells(mesh):
    cid = cell.index()
    xmid = cell.midpoint().x()
    rho_vals[cid] = rho
    if xmid < L/2.0:
        left_cells += 1
        s0_vals[cid] = -1.0e6
        initial_stress_cells += 1
    else:
        right_cells += 1
        s0_vals[cid] = 0.0

rho_fun.vector().set_local(rho_vals)
rho_fun.vector().apply("insert")
s0_fun.vector().set_local(s0_vals)
s0_fun.vector().apply("insert")

if left_cells <= 0 or right_cells <= 0 or initial_stress_cells <= 0:
    raise RuntimeError("zero required DG0 regions")

sigma0 = as_tensor(((s0_fun, 0.0, 0.0),
                    (0.0, 0.0, 0.0),
                    (0.0, 0.0, 0.0)))

def fixed_left(x, on_boundary):
    return on_boundary and near(x[0], 0.0)

bcs = [DirichletBC(V, Constant((0.0, 0.0, 0.0)), fixed_left)]
for i, bc in enumerate(bcs):
    fixed = len(bc.get_boundary_values())
    print("BC %d fixed dofs = %d" % (i, fixed))
    if fixed <= 0:
        raise RuntimeError("boundary condition fixes zero dofs")

u = TrialFunction(V)
v = TestFunction(V)
u_sol = Function(V)
body = as_vector((0.0, 0.0, -rho_fun*g))

a = inner(sigma_elastic(u), eps(v))*dx
Lform = dot(body, v)*dx - inner(sigma0, eps(v))*dx

A = assemble(a, keep_diagonal=True)
b = assemble(Lform)
for bc in bcs:
    bc.apply(A, b)
A.ident_zeros()

try:
    solve(A, u_sol.vector(), b, "mumps")
except RuntimeError:
    solve(A, u_sol.vector(), b, "lu")

stress = sigma_elastic(u_sol)
dev_stress = stress - (1.0/3.0)*tr(stress)*Identity(3)
von_mises_expr = sqrt(3.0/2.0*inner(dev_stress, dev_stress))
Q = FunctionSpace(mesh, "CG", 1)
von_mises = project(von_mises_expr, Q)

File(os.path.join(output_dir, "template_disp.pvd")) << u_sol
File(os.path.join(output_dir, "template_vonmises.pvd")) << von_mises

probe = Point(L*0.9, 0.0, H*0.5)
try:
    probe_u = u_sol(probe)
    probe_uz = float(probe_u[2])
except Exception as exc:
    print("WARNING: point probe failed: %s" % exc)
    probe_uz = float(u_sol.vector().min())

csv_path = os.path.join(output_dir, "template_nodes.csv")
with open(csv_path, "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["name", "x", "y", "z", "uz_m"])
    writer.writerow(["probe", probe.x(), probe.y(), probe.z(), probe_uz])

max_vm = float(von_mises.vector().max())
if (not math.isfinite(probe_uz)) or (not math.isfinite(max_vm)):
    raise RuntimeError("non-finite result")

print("--- FENICS JOB RESULT ---")
print(json.dumps({
    "converged": True,
    "probe_uz_m": probe_uz,
    "max_von_mises_pa": max_vm,
    "left_cells": int(left_cells),
    "right_cells": int(right_cells),
    "initial_stress_cells": int(initial_stress_cells),
    "output_files": [
        "template_disp.pvd",
        "template_vonmises.pvd",
        "template_nodes.csv"
    ]
}, ensure_ascii=False))

# END_OF_SCRIPT


