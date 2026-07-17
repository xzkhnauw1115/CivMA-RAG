# FEniCS 2019 可靠代码模式卡

本文件只记录在 FEniCS 2019.1.0 / dolfin 旧接口中稳定的写法。Coder 生成或修复脚本时，应优先采用这里的模式，避免使用 DOLFINx、petsc4py 高级接口或未经验证的花哨写法。

## 线弹性 3D 静力最小稳定骨架

适用：实体梁、桥墩、等效连续体、线性静力。

```python
from dolfin import *
import json, os

mesh = BoxMesh(Point(0, 0, 0), Point(1, 1, 1), 10, 4, 4)
V = VectorFunctionSpace(mesh, "CG", 1)

E = 3.45e10
nu = 0.20
mu = E / (2.0*(1.0 + nu))
lmbda = E*nu / ((1.0 + nu)*(1.0 - 2.0*nu))

def eps(u):
    return sym(grad(u))

def sigma(u):
    return lmbda*tr(eps(u))*Identity(3) + 2.0*mu*eps(u)

def fixed(x, on_boundary):
    return on_boundary and near(x[0], 0.0)

bcs = [DirichletBC(V, Constant((0.0, 0.0, 0.0)), fixed)]

u = TrialFunction(V)
v = TestFunction(V)
u_sol = Function(V)
f = Constant((0.0, 0.0, -1.0e4))

a = inner(sigma(u), eps(v))*dx
L = dot(f, v)*dx

A = assemble(a, keep_diagonal=True)
b = assemble(L)
for bc in bcs:
    bc.apply(A, b)
A.ident_zeros()
solve(A, u_sol.vector(), b, "mumps")
```

必须点：
- 位移场使用 `VectorFunctionSpace(mesh, "CG", 1)`。
- 线弹性应变用 `sym(grad(u))`，不要手写错误的分量矩阵。
- 大模型优先用显式组装：`assemble(..., keep_diagonal=True)`、`bc.apply(A, b)`、`A.ident_zeros()`。
- 若 `mumps` 不可用，可回退 `"lu"`，但不要混用 DOLFINx 的 `LinearProblem`。

## DG0 分区参数模式

适用：分区密度、分区弹性模量、初应力、材料标记统计。

```python
DG0 = FunctionSpace(mesh, "DG", 0)
cell_values = Function(DG0)
values = cell_values.vector().get_local()

for cell in cells(mesh):
    cid = cell.index()
    xc = cell.midpoint().x()
    zc = cell.midpoint().z()
    values[cid] = 1.0 if zc > 0.5 else 0.0

cell_values.vector().set_local(values)
cell_values.vector().apply("insert")
```

必须点：
- DG0 每个单元一个自由度时，`cell.index()` 通常可直接作为向量下标；不要对 DG0 乱用 `dofmap().cell_dofs(cell)`，容易触发类型错误。
- 写入后必须调用 `vector().apply("insert")`。
- 统计单元数量必须统计实际数组值，不得用总单元数冒充。

## 初应力场稳定写法

适用：预应力等效、温度等效初应力、残余应力。

```python
s0 = Function(DG0)
s0_values = s0.vector().get_local()
# 按单元中心赋值
s0.vector().set_local(s0_values)
s0.vector().apply("insert")

sigma0 = as_tensor(((s0, 0.0, 0.0),
                    (0.0, 0.0, 0.0),
                    (0.0, 0.0, 0.0)))

a = inner(sigma(u), eps(v))*dx
L = dot(f, v)*dx - inner(sigma0, eps(v))*dx
```

约定：
- 若总势能写成 `Pi = 1/2 eps:C:eps - f.u - sigma0:eps`，弱式右端为 `L = body - inner(sigma0, eps(v))*dx`。
- 不要把 `sigma0` 写成 Voigt 向量；FEniCS 变分中应使用二阶张量。
- 初应力符号必须在脚本注释中说明。压应力常用负号时，应通过一个小网格测试确认位移方向。

## 点取值与输出模式

```python
def safe_probe(u_fun, point, fallback_name):
    try:
        val = u_fun(point)
        return [float(val[0]), float(val[1]), float(val[2])]
    except Exception as exc:
        print("WARNING: point probe failed for %s: %s" % (fallback_name, exc))
        arr = u_fun.vector().get_local()
        return [float(arr.min()), float(arr.max()), 0.0]

File(os.path.join(output_dir, "disp.pvd")) << u_sol
print("--- FENICS JOB RESULT ---")
print(json.dumps({"converged": True}, ensure_ascii=False))
```

必须点：
- 点取值要落在实体网格内部，不要取几何边界外或空隙区。
- 结果必须打印 `--- FENICS JOB RESULT ---` 后接一行 JSON。
- 输出目录必须 `os.makedirs(output_dir, exist_ok=True)`。


