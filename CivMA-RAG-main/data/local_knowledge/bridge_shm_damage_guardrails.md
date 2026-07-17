# SHM 损伤识别参数扫描规则卡

Keywords: SHM, damage identification, stiffness inversion, damage scan, generalized eigenvalue, Hermite beam element, modal tracking, stiffness loss, 损伤识别, 刚度反演, 特征值, 频率下降, FEniCS 2019.1.0.

本卡用于长跨连续梁桥的刚度损伤反演参数扫描。目标不是获得局部三维应力，而是稳定计算“损伤刚度折减 alpha 与一阶频率下降率”的关系。

## 优先建模路线

优先使用 1D Euler-Bernoulli Hermite 梁单元矩阵代理，而不是三维实体特征值硬算。

原因：
- 三维粗网格特征值容易被桥墩弯曲、局部扭转、刚体近零模态污染。
- `CG2` 标量空间直接写 `w.dx(0).dx(0)` 不是可靠梁单元实现，容易得到 kHz 级伪频率。
- SHM 参数扫描关注相对频率下降和单调趋势，1D 梁矩阵代理更稳定、更容易验证。

脚本仍可作为 FEniCS 2019 环境下运行的 Python 脚本：保留 `from dolfin import *` 以确认环境，但核心特征值扫描可用 NumPy 矩阵完成。

## 几何与损伤区域

坐标必须严格使用任务给定范围：

- `60.0 <= x <= 90.0`
- 仅主梁区域
- 不得把损伤区移动到跨中 `x=140`

脚本必须输出：

```python
damage_region_note = "coordinates_followed: x in [60,90], beam/girder only"
```

损伤单元数量必须大于 0。

## Hermite 梁单元强制规则

每个节点 2 个自由度：竖向位移 `w` 与转角 `theta`。

单元刚度矩阵：

```python
ke = EI / Le**3 * np.array([
    [12, 6*Le, -12, 6*Le],
    [6*Le, 4*Le**2, -6*Le, 2*Le**2],
    [-12, -6*Le, 12, -6*Le],
    [6*Le, 2*Le**2, -6*Le, 4*Le**2],
])
```

一致质量矩阵：

```python
me = m_line * Le / 420.0 * np.array([
    [156, 22*Le, 54, -13*Le],
    [22*Le, 4*Le**2, 13*Le, -3*Le**2],
    [54, 13*Le, 156, -22*Le],
    [-13*Le, -3*Le**2, -22*Le, 4*Le**2],
])
```

边界条件：
- 在 `x = 0, 72.5, 207.5, 280` 约束竖向位移 `w=0`。
- 不约束转角 `theta`，否则会把简支/连续支承误写成固结，频率会偏高。
- 必须输出 `support_w_dofs` 和 `free_dofs`。

## 损伤扫描规则

- alpha 扫描固定为 `[1.0, 0.9, 0.8, 0.7, 0.6]`。
- 损伤只改变刚度，不改变质量。
- 对损伤区单元：`EI_alpha = alpha * EI_base`。
- 对非损伤区单元：`EI_alpha = EI_base`。
- 每个 alpha 都重新组装刚度矩阵，质量矩阵可复用。

频率计算：

```python
A = np.linalg.solve(M_free, K_free)
eigvals = np.linalg.eigvals(A).real
positive = sorted(v for v in eigvals if v > 1.0e-10)
f1 = math.sqrt(positive[0]) / (2.0 * math.pi)
```

## 必须验证的物理趋势

Researcher 必须拒绝以下结果：
- `f0_Hz <= 0` 或非有限数。
- 频率达到几十 Hz、几百 Hz、kHz 量级，通常说明刚度/质量单位或边界错误。
- alpha 越小，频率反而升高。
- `df_pct` 非单调，说明没有追踪同一类一阶弯曲模态。
- 损伤区单元数为 0。
- 质量矩阵随 alpha 改变。

注意：目标下降率 `target_df_pct = 5.0` 不一定能在 `[1.0, 0.6]` 内达到。如果最大下降率小于 5%，应选择误差最小的 alpha，并说明“扫描范围未覆盖目标下降率”，不能强行伪造达到目标。

## 输出协议

CSV 文件名建议：`bridge_shm_damage_scan.csv`

CSV 列：
- `alpha`
- `f1_Hz`
- `df_pct`

JSON 必须包含：

```json
{
  "converged": true,
  "analysis_type": "shm_damage_eigen_scan",
  "model_level": "hermite_beam_matrix_surrogate",
  "f0_Hz": 0.0,
  "target_df_pct": 5.0,
  "best_alpha": 1.0,
  "stiffness_degradation_pct": 0.0,
  "alpha_steps_completed": 5,
  "damaged_elements": 1,
  "support_w_dofs": 4,
  "free_dofs": 1,
  "monotonic_frequency_drop": true,
  "damage_region_note": "coordinates_followed: x in [60,90], beam/girder only",
  "output_files": ["bridge_shm_damage_scan.csv"]
}
```

