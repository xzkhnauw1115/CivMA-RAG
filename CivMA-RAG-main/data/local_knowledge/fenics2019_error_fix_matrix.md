# FEniCS 2019 错误-修复对照表

本表用于 Coder 修复失败脚本。原则：先识别错误类型，再完整重写脚本，不做零散行号补丁。

## TypeError: cell_dofs(): incompatible function arguments

常见原因：
- 把 `cell` 对象直接传给 `cell_dofs()`，而该接口期望单元编号。
- 对 DG0 空间过度使用 dofmap，导致不同版本不兼容。

稳定修复：
```python
DG0 = FunctionSpace(mesh, "DG", 0)
q = Function(DG0)
vals = q.vector().get_local()
for cell in cells(mesh):
    cid = cell.index()
    vals[cid] = 1.0
q.vector().set_local(vals)
q.vector().apply("insert")
```

不要写：
```python
dofs = DG0.dofmap().cell_dofs(cell)  # 错
```

## Unable to evaluate expression

常见原因：
- `UserExpression.eval()` 没有给所有分量赋值。
- `value_shape()` 缺失或返回维度错误。
- `Expression` C++ 字符串里用了 Python 函数或中文符号。
- 点取值 `u(Point(...))` 落在网格外。

稳定修复：
```python
class BodyForce(UserExpression):
    def eval(self, values, x):
        values[0] = 0.0
        values[1] = 0.0
        values[2] = -1.0
    def value_shape(self):
        return (3,)
```

若只是分区常数，优先使用 DG0 `Function`，不要使用复杂 `UserExpression`。

## missing # END_OF_SCRIPT marker / unexpected EOF

常见原因：
- LLM 输出被截断。
- Coder 只追加了脚本尾标记，没有写完整脚本。

稳定修复：
- 必须先 RAG。
- 调用 `reset_current_fenics_script()`。
- 一次性或分块写入完整脚本。
- 最后一行必须是独立的 `# END_OF_SCRIPT`。
- 运行前必须调用 `get_current_fenics_script_status()`，确认：
  - `syntax_ok == true`
  - `has_end_marker == true`
  - `lines` 足够，不是空壳脚本。

## NameError: name 'out_dir' is not defined

常见原因：
- 输出目录变量名混用：`out_dir`、`output_dir`、`result_dir`。

稳定修复：
```python
output_dir = os.getcwd()
os.makedirs(output_dir, exist_ok=True)
File(os.path.join(output_dir, "disp.pvd")) << u_sol
```

规则：全脚本只使用一个输出目录变量名 `output_dir`。

## Solver failed / did not converge / singular matrix

常见原因：
- 边界条件没有固定任何自由度。
- 使用了空隙区极低刚度实体参与主方程，导致病态矩阵。
- 材料区单元数为 0。
- 预应力区单元数为 0。

求解前必须检查：
```python
for i, bc in enumerate(bcs):
    nvals = len(bc.get_boundary_values())
    print("BC %d fixed dofs = %d" % (i, nvals))
    if nvals == 0:
        raise RuntimeError("boundary condition fixes zero dofs")

if beam_cells <= 0 or left_pier_cells <= 0 or right_pier_cells <= 0:
    raise RuntimeError("zero structural material cells")
```

稳定求解模式：
```python
A = assemble(a, keep_diagonal=True)
b = assemble(L)
for bc in bcs:
    bc.apply(A, b)
A.ident_zeros()
solve(A, u_sol.vector(), b, "mumps")
```

## Point is not inside domain

常见原因：
- 关键点坐标在边界上、空隙区、或网格外。

稳定修复：
- 点取值选择实体内部点。
- 顶板点不要正好取 `z=131.0` 的外边界，可取 `z=130.5`。
- 若点取值失败，必须打印警告，不得编造结果。

## von Mises 应力异常巨大

常见原因：
- 把空隙区或低刚度 ghost 区域应力计入最大值。
- 在尖角、约束点、分区跳变界面读取局部奇异峰值。
- 投影空间选择不当导致数值振荡。

稳定处理：
- 只在真实结构材料区统计应力。
- 若使用标记或 DG0 mask，最大值统计必须乘以结构区 mask 或单独遍历结构单元。
- Researcher 验证时应同时看位移量级、材料区最大应力、预应力单元数量，不得只看脚本是否能运行。

## 版本混用错误

禁止在 FEniCS 2019 脚本中使用：
- `dolfinx`
- `ufl.Measure` 的 DOLFINx 写法
- `fem.petsc.LinearProblem`
- `mesh.topology.dim`
- `locate_dofs_geometrical`

必须使用：
- `from dolfin import *`
- `VectorFunctionSpace`
- `FunctionSpace(mesh, "DG", 0)`
- `DirichletBC`
- `MeshFunction` / `SubDomain`
- `File("x.pvd") << function`


