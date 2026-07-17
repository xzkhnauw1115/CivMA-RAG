"""
Golden/RAG guardrails for reference bridge Static prestress task (FEniCS 2019.1.0).
This file is intentionally a compact reference script skeleton plus hard rules.
Agents must adapt it into temp_scripts/fenics_drafts/current_fenics_script.py.

Key lessons from failed runs:
1. Never bypass RAG. Fetch this file before coding/fixing.
2. Dynamic bottom slab prestress: z_bottom(x) <= z <= z_bottom(x)+2.0, not a fixed 118~120 band.
3. Count top/bottom prestress cells from sigma0 DG0 values. Do not fake counts with beam cell count.
4. Abort before solve when material or prestress cell counts are zero.
5. Emit --- FENICS JOB RESULT --- followed by one JSON line.
6. Reject physically impossible results: abs(mid_span_uz_m) > 1.0 m for this test model.
"""
from dolfin import *
import json
import os
import csv

# Stable FEniCS 2019 idioms to preserve in generated scripts:
# V = VectorFunctionSpace(mesh, "CG", 1 or 2)
# DG0 = FunctionSpace(mesh, "DG", 0)
# A = assemble(a, keep_diagonal=True); b = assemble(L)
# for bc in bcs: bc.apply(A, b)
# A.ident_zeros(); solve(A, u_sol.vector(), b, "mumps")
# File(os.path.join(output_dir, "bridge_static_prestress_disp.pvd")) << u_sol

L_total = 280.0
W_half = 6.0
Z_TOP = 131.0
Z_PIER_TOP = 120.0

# User-provided bridge-bottom geometry. Use this for prestress regions and point checks.
def beam_height(x):
    if x <= 72.5:
        return 11.0 - (11.0 - 4.5) * (1.0 - x / 72.5) ** 2
    if x <= 207.5:
        xi = 2.0 * abs(x - 140.0) / 135.0
        return 8.0 + (11.0 - 8.0) * xi ** 2
    xr = x - 207.5
    return 11.0 - (11.0 - 4.5) * (1.0 - xr / 72.5) ** 2

def z_bottom(x):
    return Z_TOP - beam_height(x)

def alpha_EI_from_height(h):
    # Clamp between midspan and root values from the prompt.
    if h <= 8.0:
        return 0.712
    if h >= 11.0:
        return 0.872
    return 0.712 + (h - 8.0) / 3.0 * (0.872 - 0.712)

def rho_eff_from_height(h):
    # Equivalent density from alpha_EA: 344 kg/m3 at h=8, 584 kg/m3 at h=11.
    if h <= 8.0:
        return 344.0
    if h >= 11.0:
        return 584.0
    return 344.0 + (h - 8.0) / 3.0 * (584.0 - 344.0)

def assign_sigma0_for_cell(xc, zc):
    """Return sigma0_xx for beam cells only; caller must skip non-beam cells."""
    zb = z_bottom(xc)
    if 129.0 <= zc <= Z_TOP:
        return -4.0e6
    if zb <= zc <= zb + 2.0:
        return -15.0e6
    return 0.0

def must_abort_before_solve(beam_cells, left_pier_cells, right_pier_cells, top_cells, bottom_cells):
    checks = {
        "beam_cells": beam_cells,
        "left_pier_cells": left_pier_cells,
        "right_pier_cells": right_pier_cells,
        "top_prestress_cells": top_cells,
        "bottom_prestress_cells": bottom_cells,
    }
    bad = [k for k, v in checks.items() if int(v) <= 0]
    if bad:
        print("CRITICAL ERROR: zero required regions: " + ", ".join(bad))
        print("--- FENICS JOB RESULT ---")
        print(json.dumps({"converged": False, "error": "zero_required_regions", "bad_regions": bad}))
        exit(1)

def print_static_prestress_json(**kwargs):
    """Generated scripts should call this pattern after writing pvd/csv/txt outputs."""
    print("--- FENICS JOB RESULT ---")
    print(json.dumps(kwargs, ensure_ascii=False))

# Researcher validation hard stop:
# if abs(mid_span_uz_m) > 1.0 -> verification failed, do not TERMINATE.
# if max_von_mises_pa is missing or from non-structure cells -> verification failed.

