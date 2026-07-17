# 物理验证规则卡

本文件给 Researcher 和 Coder 使用。目标是识别“脚本能跑但物理不可信”的结果，避免把形式正确误判为物理验证通过。

## 通用验证原则

仿真结果必须同时满足：
- 脚本运行成功，`converged == true`。
- 输出 JSON 中包含关键物理量，不得缺失。
- 关键单元/区域计数大于 0。
- 位移、应力、反力或能量的量级符合工程直觉。
- 输出文件确实写入，而不是只在 JSON 中列出文件名。

若最近一次仿真状态为 failed，不允许记录“物理验证通过”。

## 线弹性静力验证

必须检查：
- 最大位移不是 `nan`、`inf`、`1e10` 等奇异值。
- 最大位移相对结构跨度不应离谱。普通桥梁粗网格测试中，跨中位移若超过 1 m 通常必须判失败。
- von Mises 应力不应远超材料强度几个数量级。
- 若应力峰值只出现在单个约束点或尖角，应说明“局部奇异”，不能直接宣称整体结构失效。

推荐 JSON 字段：
```json
{
  "converged": true,
  "mid_span_uz_m": 0.0,
  "side_span_uz_m": 0.0,
  "pier_top_ux_m": 0.0,
  "max_von_mises_pa": 0.0,
  "beam_cells": 1,
  "left_pier_cells": 1,
  "right_pier_cells": 1
}
```

## 预应力/初应力验证

必须检查：
- 初应力区单元数必须大于 0。
- 顶板区和底板区单元数必须分开统计，不得用主梁总单元数冒充。
- 初应力符号与位移方向需要解释：压应力取负时，变分式中符号必须自洽。
- 施加预应力后，跨中挠度相对无预应力应显著变化；若完全无变化，通常说明初应力没有进入弱式。

推荐 JSON 字段：
```json
{
  "top_prestress_cells": 1,
  "bottom_prestress_cells": 1,
  "prestress_min_pa": -15000000.0,
  "prestress_max_pa": 0.0
}
```

## 分区材料验证

必须检查：
- 主梁、左墩、右墩单元数量均大于 0。
- 密度和弹性模量不得全部为默认值，若需求要求分区折减，JSON 或日志中应输出折减范围。
- 空隙区不能控制结构刚度。若使用 bounding box ghost mesh，必须只在结构区统计应力和关键点。

## 输出文件验证

脚本应写出：
- 位移场 `.pvd`
- 应力或 von Mises `.pvd`
- 关键节点/点位移 `.csv`
- 需要时写出配筋、预应力说明 `.txt`

Researcher 验证时，不能仅因为 JSON 中出现文件名就判定成功；应检查后端结果目录是否存在这些文件，或脚本中确实有 `File(os.path.join(output_dir, ...)) << ...` 和 `open(..., "w")`。

## 失败判据

任一情况出现，应判 `验证失败`：
- 最近一次仿真未完成。
- `converged` 不是 true。
- 关键 JSON 字段缺失。
- 结构材料单元或预应力单元计数为 0。
- 位移为 `nan/inf` 或绝对值超过合理上限。
- 应力明显来自空隙区、约束奇异点，且没有做结构区过滤。
- 脚本只是形式上包含函数名，但没有把对应物理项加入变分式。
## 动力时程验证

适用于移动荷载动力任务。必须检查：

- `time_steps_completed` 必须等于 40。
- 质量矩阵必须包含密度；若脚本中出现无密度的 `assemble(inner(u, v)*dx)` 作为主质量矩阵，应判失败。
- 移动荷载作用区域计数必须大于 0。
- `u_z_static_m`、`u_z_max_m`、`dynamic_static_ratio` 必须存在且有限。
- 若静力位移分母接近 0，动静位移比必须显式保护，不能输出误导性巨大比值。
- CSV 必须含时间列和跨中位移列，步数应与 Newmark 步数一致。

推荐 JSON 字段：
```json
{
  "analysis_type": "implicit_newmark_dynamics",
  "time_steps_completed": 40,
  "u_z_static_m": 0.0,
  "u_z_max_m": 0.0,
  "t_max_s": 0.0,
  "dynamic_static_ratio": 0.0,
  "top_load_facets_or_cells": 1
}
```

## 特征值和风速扫描验证

适用于颤振扫描任务。必须检查：

- 基准一阶频率 `f0_Hz` 必须为正且有限。
- 风速扫描必须覆盖 20 到 120 m/s、步长 5 m/s，共 21 步。
- 气动刚度必须是装配得到的矩阵或双线性形式，不得把标量直接从刚度矩阵中相减。
- 非正特征值表示失稳或不稳定状态，不能继续 `sqrt` 后伪装成实频率。
- `aero_facets_or_cells` 必须大于 0。
- 墩底固结和梁端竖向约束必须输出真实 DOF 数；不得用单元数量估算。若 `base_fixed_dofs == 0` 或 `end_vertical_dofs == 0`，必须判验证失败。

推荐 JSON 字段：
```json
{
  "analysis_type": "scanlan_flutter_eigen_scan",
  "f0_Hz": 0.0,
  "wind_steps_completed": 21,
  "U_cr_mps": null,
  "max_df_pct": 0.0,
  "aero_facets_or_cells": 1
}
```

## SHM 损伤反演验证

适用于SHM损伤识别任务。必须检查：

- 损伤坐标必须使用 `x in [60,90]` 且仅主梁单元，不得移动到 `x=140`。
- `damaged_cells` 必须大于 0。
- alpha 扫描必须包含 `1.0, 0.9, 0.8, 0.7, 0.6` 五个点。
- 质量矩阵不能随 alpha 折减；损伤扫描只改刚度。
- `best_alpha` 必须来自与 `target_df_pct=5.0` 的误差比较。

推荐 JSON 字段：
```json
{
  "analysis_type": "shm_damage_eigen_scan",
  "f0_Hz": 0.0,
  "target_df_pct": 5.0,
  "best_alpha": 1.0,
  "stiffness_degradation_pct": 0.0,
  "alpha_steps_completed": 5,
  "damaged_cells": 1
}
```




