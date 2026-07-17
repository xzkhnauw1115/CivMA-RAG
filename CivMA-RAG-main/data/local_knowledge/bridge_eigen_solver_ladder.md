# 目标桥梁颤振扫描任务特征值求解器阶梯路线 / Flutter scan task eigen solver ladder

Keywords: 颤振扫描任务, flutter_scan, SLEPc, eigenvalue, zero wind, no eigenvalues converged, timeout, modal smoke test, solver ladder, 颤振, 特征值不收敛, 目标桥梁.

本卡专门处理颤振扫描任务反复失败的问题。目标不是让 Coder 继续盲目调 SLEPc 参数，而是按可验证的求解阶梯逐级推进。

## 失败模式复盘

最近颤振扫描任务失败主要有四类：

1. `base_fixed_dofs == 0`：粗网格节点没有落在 4 m 墩宽范围内，基准特征值问题存在刚体模态。
2. `TypeError: SLEPcEigenSolver(K, M)`：传入普通 `Matrix`，不是 `PETScMatrix`。
3. `nconv == 0` 或 `No eigenvalues converged at zero wind`：3D bounding-box 连续体 + 极端 void/ghost 参数导致广义特征值病态。
4. 长时间 404/超时：一上来使用较大 3D 网格和 10 个模态，SLEPc 计算太慢。

因此颤振扫描任务必须先通过“零风速基准模态烟测”，再进入风速扫描。

## 强制求解阶梯

### Level 0: API 和边界烟测

必须先满足：

- `base_fixed_dofs > 0`，且来自 `bc.get_boundary_values()` 的真实 DOF 数。
- `end_vertical_dofs > 0`，且来自真实 DOF 数。
- `aero_facets_or_cells > 0`。
- `K_struct` 和 `M` 必须是 `PETScMatrix`。
- 打印 `K_norm`、`M_norm`、实际 DOF 数、结构单元数。

推荐装配方式：

```python
K_struct = PETScMatrix()
M = PETScMatrix()
assemble(a_struct, tensor=K_struct)
assemble(m_form, tensor=M)
```

不要使用：

```python
as_backend_type(assemble(a_struct), "petsc")
```

这在 FEniCS 2019 中是错误 API。

### Level 1: 最小 3D 基准模态烟测

先只求零风速 `f0`，不要风速扫描。

硬限制：

- `nx <= 40`, `ny <= 2`, `nz <= 10`。
- `V.dim() <= 12000`。
- 只求 `solve(3)` 或更少。
- 若 60 秒内不能得到 `nconv > 0`，不得继续加大网格；必须换简化模型。

SLEPc 推荐设置：

```python
eps = SLEPcEigenSolver(K_struct, M)
eps.parameters["problem_type"] = "gen_hermitian"
eps.parameters["spectrum"] = "target magnitude"
eps.parameters["spectral_transform"] = "shift-and-invert"
eps.parameters["spectral_shift"] = 1.0e-4
eps.parameters["tolerance"] = 1.0e-6
eps.parameters["maximum_iterations"] = 1000
eps.solve(3)
```

如果 `target magnitude` 在当前环境不可用，退回：

```python
eps.parameters["spectrum"] = "smallest magnitude"
```

但不要在同一个脚本里无限尝试很多求解器组合。

### Level 2: 1D 等效梁/扭转代理模型

如果 Level 1 仍 `nconv == 0` 或超时，颤振扫描任务应切换为 1D 等效模态代理模型，而不是继续 3D 硬算。

这是允许的，因为用户颤振扫描任务本身是 Scanlan 简化模型，关键输出是风速扫描下的一阶频率下降和临界风速，不是 3D 局部应力。

1D 代理模型要求：

- 使用 `IntervalMesh` 或小型 FEniCS 标量/向量空间。
- 使用等效质量 `m_line = rho_eff * A_equiv`。
- 使用等效弯曲刚度 `EI_eff = E_eff * I_equiv` 或等效扭转刚度代理。
- 零风速频率可用矩阵特征值或解析梁频率估计。
- Scanlan 气动刚度仍按用户公式进入扫描。
- JSON 中必须写明 `model_level = "1d_equivalent_modal_surrogate"`，不能伪装成完整 3D 连续体。

### Level 3: 风速扫描

只有 `f0_Hz > 0` 且有限，才允许进入风速扫描。

扫描要求：

- 生产要求：`U = 20,25,...,120` 共 21 点。
- 调试阶段可以 `U = 20,30,...,120`，但 JSON 必须写 `wind_steps_completed`，Researcher 可据此判断是否满足最终要求。
- 非正特征值表示失稳，不能 `sqrt`。

## 禁止事项

- 禁止一开始就用 `nx=280` 或更大 3D 网格跑 SLEPc。
- 禁止在 `nconv==0` 后直接宣布结果。
- 禁止跳过 `get_current_fenics_script_status()` 直接 `run_current_fenics_script()`。
- 禁止把 `status: completed` 当作 `converged: true` 的替代字段。
- 禁止只输出 `zero_wind_frequency_Hz`，必须同时输出颤振扫描任务规定字段 `f0_Hz`。

## 颤振扫描任务最低可接受 JSON

```json
{
  "converged": true,
  "analysis_type": "scanlan_flutter_eigen_scan",
  "model_level": "3d_smoke_test_or_1d_equivalent_modal_surrogate",
  "f0_Hz": 0.0,
  "U_cr_mps": null,
  "max_df_pct": 0.0,
  "wind_steps_completed": 21,
  "base_fixed_dofs": 1,
  "end_vertical_dofs": 1,
  "aero_facets_or_cells": 1,
  "output_files": ["bridge_flutter_scan.csv"]
}
```


