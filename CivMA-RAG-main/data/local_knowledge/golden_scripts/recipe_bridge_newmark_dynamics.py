"""
FEniCS 2019.1.0 golden recipe for reference bridge moving-load dynamics task.

Purpose:
- Stable Newmark-beta matrix/vector update pattern for linear dynamics.
- Density-weighted mass matrix, self-weight static initialization, moving load hook.
- This is a recipe, not a complete bridge model.
"""

from dolfin import *
import math

BETA_NEWMARK = 0.25
GAMMA_NEWMARK = 0.5
DT = 0.1
T_TOTAL = 4.0
N_STEPS = int(round(T_TOTAL/DT))
TRAIN_SPEED = 80.0
LOAD_AMP_N = 100000.0


def assemble_effective_newmark_matrix(K, M, bcs, beta=BETA_NEWMARK, dt=DT):
    """Return K_eff = K + M/(beta*dt^2), with BCs applied."""
    K_eff = K.copy()
    K_eff.axpy(1.0/(beta*dt*dt), M, True)
    for bc in bcs:
        bc.apply(K_eff)
    return K_eff


def newmark_predictor_rhs(F_total, M, u_n, v_n, a_n, beta=BETA_NEWMARK, dt=DT):
    """No-damping Newmark RHS vector: F + M*(a0*u + a2*v + a3*a)."""
    a0 = 1.0/(beta*dt*dt)
    a2 = 1.0/(beta*dt)
    a3 = 1.0/(2.0*beta) - 1.0
    rhs = F_total.copy()
    inertial = u_n.copy()
    inertial *= a0
    tmp = v_n.copy()
    tmp *= a2
    inertial.axpy(1.0, tmp)
    tmp = a_n.copy()
    tmp *= a3
    inertial.axpy(1.0, tmp)
    rhs.axpy(1.0, M*inertial)
    return rhs


def update_newmark_state(u_np1, u_n, v_n, a_n, beta=BETA_NEWMARK, gamma=GAMMA_NEWMARK, dt=DT):
    """Return updated acceleration and velocity vectors."""
    a0 = 1.0/(beta*dt*dt)
    a2 = 1.0/(beta*dt)
    a3 = 1.0/(2.0*beta) - 1.0
    a_np1 = u_np1.copy()
    a_np1.axpy(-1.0, u_n)
    a_np1 *= a0
    tmp = v_n.copy()
    tmp *= a2
    a_np1.axpy(-1.0, tmp)
    tmp = a_n.copy()
    tmp *= a3
    a_np1.axpy(-1.0, tmp)

    v_np1 = v_n.copy()
    tmp = a_n.copy()
    tmp *= dt*(1.0 - gamma)
    v_np1.axpy(1.0, tmp)
    tmp = a_np1.copy()
    tmp *= dt*gamma
    v_np1.axpy(1.0, tmp)
    return v_np1, a_np1


class MovingGaussianTopLoad(UserExpression):
    """Pressure/traction shape. Use in -p*v[2]*ds_top or convert carefully to a volume load."""
    def __init__(self, t=0.0, amp=LOAD_AMP_N, speed=TRAIN_SPEED, **kwargs):
        super().__init__(**kwargs)
        self.t = float(t)
        self.amp = float(amp)
        self.speed = float(speed)

    def eval(self, value, x):
        xc = self.speed*self.t
        if -2.0 <= x[1] <= 2.0:
            value[0] = self.amp*math.exp(-((x[0] - xc)**2)/10.0)
        else:
            value[0] = 0.0

    def value_shape(self):
        return ()


REQUIRED_CASE5_JSON_FIELDS = [
    "converged",
    "analysis_type",
    "time_steps_completed",
    "dt_s",
    "u_z_static_m",
    "u_z_max_m",
    "t_max_s",
    "dynamic_static_ratio",
    "beam_cells",
    "left_pier_cells",
    "right_pier_cells",
    "top_load_facets_or_cells",
    "base_fixed_dofs",
    "end_vertical_dofs",
    "output_files",
]


