# Active golden scripts

These templates are the only preferred local code recipes for RAG-assisted generation.

- `recipe_fenics2019_ghost_staged_static.py`: staged construction with ghost material activation.
- `recipe_bridge_static_prestress_guardrails.py`: static gravity + reinforcement + prestress initial-stress template.
- `recipe_bridge_thermal_coupling_guardrails.py`: thermal-gradient thermoelastic weak-form template.
- `recipe_bridge_pdelta_guardrails.py`: geometric nonlinear P-Delta comparison template.
- `recipe_bridge_newmark_dynamics.py`: implicit Newmark moving-load dynamics template.
- `recipe_bridge_flutter_scan.py`: Scanlan-style flutter/eigen scan template.
- `recipe_bridge_shm_damage_scan.py`: SHM stiffness-loss scan using a stable Hermite beam matrix surrogate.

Do not use generic PDE demos, unrelated bridge geometry, stale output protocols, or snippets that do not print `--- FENICS JOB RESULT ---` JSON.
