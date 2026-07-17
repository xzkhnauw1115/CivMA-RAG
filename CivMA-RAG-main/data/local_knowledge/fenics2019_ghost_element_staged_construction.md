# FEniCS 2019 悬臂施工任务 幽灵单元法与分阶段施工建模规则

关键词: 悬臂施工任务, 幽灵单元, Ghost Element Method, staged construction, 悬臂施工, 材料激活, DG0, UserExpression, BoxMesh, FEniCS 2019.1.0

## 适用场景

本知识卡用于 72.5+135+72.5m 连续刚构桥的施工阶段拟静力分析。核心目标是在同一套 BoxMesh 上用幽灵单元保持矩阵可解，同时通过阶段材料和密度控制表达结构逐步激活。

## 正确建模原则

1. 幽灵单元不是空单元删除。FEniCS 2019 的 BoxMesh 不支持在求解时真正删除单元，未激活区应保留极小刚度 `E_ghost`，常用 `1.0 Pa` 或 `max(1.0, 1e-10*E_c)`。
2. 未激活区不能施加自重。否则会产生“幽灵结构自重”，导致阶段0和阶段1位移虚假偏大。未激活区应设置 `rho=0.0`，或在体力项中按 active flag 置零。
3. 每个施工阶段独立求解，不继承上一阶段位移。实现上应在循环中重新创建 `Function(V)`、重新组装 `a` 和 `L`。
4. 阶段材料建议使用 DG0 Function 或 `UserExpression(degree=0)`，不要使用向量化 numpy 写法；`eval(self, value, x)` 只接收单个坐标点。
5. 泊松比保持稳定，例如 `nu=0.20`；由空间变化的 `E(x)` 计算 `mu=E/(2*(1+nu))` 与 `lambda=E*nu/((1+nu)*(1-2*nu))`。
6. 对全域 BoxMesh 的空隙区，也应使用 ghost stiffness 且零密度，避免刚度矩阵奇异；真实结构区才参与自重。
7. 阶段2主梁材料应采用箱梁等效 + 钢筋弥散后的 `E_final(x)`；桥墩区仍用 C50 `E_c` 和 `rho=2550`。

## 本工况阶段定义

以用户任务文本为准：

- Stage 0: bridge piers active for `z < 120m`; girder and void regions are ghost with zero density.
- Stage 1: piers plus the specified zero-block/girder segment `x in [120,160] and z >= 120` are active; other girder/void regions are ghost with zero density.
- Stage 2: completed bridge. Girder active with variable equivalent `E_final(x)` and effective density; piers active with C50 material.

注意：真实工程的 0 号块通常在墩顶附近，但如果用户任务明确给出 `x in [120,160]`，脚本应按任务执行，并在诊断输出中打印这一选择，不能擅自改成墩顶区。

## 变截面等效建议

梁高可由顶标高 `z_top=131` 与梁底曲线计算：`height(x)=z_top-bottom_z(x)`。将 `height` 在线性映射到折减系数：

- `alpha_EI(8m)=0.712`, `alpha_EI(11m)=0.872`
- `alpha_EA(8m)=0.135`, `alpha_EA(11m)=0.229`

若梁高低于 8m（边跨端部 4.5m），应进行合理限幅，避免外推出不可信材料：

```python
h_ref = min(max(height, 8.0), 11.0)
t = (h_ref - 8.0) / 3.0
alpha_EI = 0.712 + t * (0.872 - 0.712)
alpha_EA = 0.135 + t * (0.229 - 0.135)
```

普通钢筋各向同性弥散可用：

```python
rebar_factor = 1.0 + rho_avg * E_s / E_c  # rho_avg=0.0206
E_final = E_c * alpha_EI * rebar_factor
rho_eff = rho_c * alpha_EA
```

## FEniCS 2019 稳定实现要点

1. 对每个 cell 的 midpoint 判断区域，填充 DG0 `E_func` 与 `rho_func`，比复杂 `UserExpression` 更容易调试。
2. 必须打印各阶段 active cell 数、ghost cell 数、rho>0 cell 数、固定边界 DOF 数、梁端竖向约束 DOF 数。
3. 若某阶段固定边界 DOF 为 0，或 active cell 数为 0，应打印 `CRITICAL ERROR` 并退出。
4. 大模型优先显式组装：`A=assemble(a, keep_diagonal=True)`, `b=assemble(L)`, `bc.apply(A,b)`, `A.ident_zeros()`, `LUSolver(A, "default")`。
5. 输出必须包含 `--- FENICS JOB RESULT ---` 后跟 JSON，字段至少包括 `converged`, `stage_mid_span_uz_m`, `stage_active_cells`, `stage_ghost_cells`, `output_files`。
6. CSV `bridge_staged_construction_stages.csv` 三行数据：`stage,u_z_m,description`。PVD `bridge_staged_construction_disp.pvd` 输出阶段2位移。

## 常见错误

- 错误：ghost 区仍然使用真实密度。后果：阶段0/1出现不存在结构的自重。
- 错误：E=0 或 rho=0 且刚度也为0。后果：矩阵奇异或求解器失败。
- 错误：三个阶段复用同一个位移场并累加。后果：不符合“位移不跨阶段累积”的任务要求。
- 错误：把预应力静力任务预应力字段带入悬臂施工任务。悬臂施工任务明确不施加任何预应力。
- 错误：输出中伪造阶段结果。必须只从真实求解后的 `u_sol(Point(140,0,131))[2]` 或安全 fallback 得到。


