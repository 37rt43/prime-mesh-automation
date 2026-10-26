# prime-mesh-automation

基于 [PyPrimeMesh](https://github.com/ansys/pyprimemesh) 的自动化网格划分脚本仓库，配合 ChatGPT Codex 云端环境使用。

## 版本对应（务必先看）

本机为 **Ansys 2024R1（Prime Server 24.1.0）**，客户端必须用 **0.5.x**，且只支持 **Python 3.8–3.11**。

## 1. 准备 Python 3.11

本机若只有 Python 3.12/3.14，先装 3.11（PowerShell）：

```powershell
py install 3.11
```

## 2. 用 3.11 创建虚拟环境并安装依赖

```powershell
cd 你的仓库路径\prime-mesh-automation
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version          # 应显示 3.11.x
pip install -r requirements.txt
```

## 3. 验证连接

```powershell
python main.py
```

应打印 0.5.x 客户端版本和 Prime Server 进程 PID。

## 4. 导入 CAD 划分网格

```powershell
python main.py --cad part.step --min-size 0.5 --max-size 5 --out part.msh
```

脚本用 Lucid 高层接口：读入 CAD → 面网格 → 多面体体网格 → 导出 `.msh`（可直接给 Fluent）。

## 5. 接入 Codex

1. 在 GitHub 新建空仓库 `prime-mesh-automation`（不勾自动 README），上传本目录文件并提交。
2. ChatGPT → Codex → 新建云端环境 → 选择 `prime-mesh-automation` 仓库。
3. Codex 会读取 `requirements.txt` 与 `AGENTS.md`（已锁定 0.5.x / 24.1 / Python 3.11）。

> 云端 Codex 没有 Prime Server 和许可证，只负责写代码；实际划网格在本机运行。

## FFF-2 离心泵流体域自动网格

```powershell
python impeller_mesh.py --cad FFF-2.scdoc --out FFF-2.msh
```

无需输入尺寸。脚本使用 `main.py` 中的安全 `prime.launch_prime()` 启动器，
导入 CAD 后打印包围盒和整体尺寸；包围盒体积的 25% 作为初始流体体积估值，
结合关键区域包围盒特征尺寸计算基础尺寸、局部尺寸和边界层厚度。
全局曲率（15°）及邻近（间隙 4 单元）控制解析弯曲面和窄流道；叶片、进出口额外加密，
其余大面允许使用基础大尺寸。所有尺寸沿用导入 CAD 单位。

壁面设置 5 层边界层，增长率 1.2，总厚度取叶片局部尺寸的 35% 与
特征尺寸的 1.5% 中较小值，按等比数列计算第一层高度；进口、出口不生长层。
厚度为几何比例估算，不代表指定 y+，局部实际层数可能因窄缝或碰撞被后端裁剪。
体填充为 POLY，边界层使用棱柱。逐部件检查存在 POLY 及非零体单元，未生成棱柱层时打印警告。

每轮打印实际总单元数（包含边界层）；按实际数量自动调整尺寸并重新导入网格，
最多尝试 6 轮，仅在总数处于 300000–500000 时导出 `FFF-2.msh`。
若未达标、导入/网格/导出失败，抛出异常并以非零状态退出，不导出未达标结果。
旧有输出文件不会在失败时被删除，请以本次成功日志为准。

命名选择严格按以下原名查询，区分大小写，无通配符或自动识别：

- `inlet-section`、`outlet-section`：进口段、出口段。
- `inlet`、`outlet`：进出口面。
- `interface-1`、`interface-2`、`interface-3`：分别保留，禁止合并或拼接。
- `impeller`、`blade`：旋转区域，额外加密。
- `volute`、`front-cover`、`back-cover`：蜗壳、前后盖。
- `Seal_Chamber_Inlet`、`Seal_Chamber_Outlet`、`Seal_Chamber`：密封腔相关，额外加密。

直接复用已有 zone；CAD 面标签仅转换为同名面 zone。具名部件或体 zone
仅提供区域加密范围，保留原有体 zone 名称。任何名称缺失均打印警告并跳过，
不会因此停止。识别成功仅取决于名称是否存在于标签、部件或 zone 中，
不要求该名称必须能返回 face 实体；已识别但无面的情况只警告并跳过相应面操作。
启动时打印脚本版本及绝对路径，便于排查误运行旧副本。已删除封闭拓扑面的前置检查。支持拓扑面网格或已有面 zonelets 重划分。
若没有导入体积，则调用后端计算体积；后端真实的网格错误仍会报告。
脚本不会提取流体域或自动封口。

边界层只施加于已命名的叶轮、叶片、蜗壳、前后盖和密封腔壁面；
三个 interface 以及 `inlet`、`outlet`、`Seal_Chamber_Inlet`、`Seal_Chamber_Outlet`
优先排除于边界层之外。未命名面不会自动归类为壁面。
三个 interface 原名作为独立面 zone 导出，不会改为 wall 名称；
Prime 0.5 的 MSH 导出 API 不提供 Fluent 求解器边界类型设置，
Fluent 中仍需将这些 zone 配置为 interface 并建立相应网格接口配对。

```powershell
python -m py_compile main.py impeller_mesh.py
python -m unittest discover -s tests -v
```

本仓库云端验证仅覆盖脚本语法、0.5.1 API 签名及无后端逻辑。
未提供 `FFF-2.scdoc`，且云端没有 Prime Server/许可证，因此尚未生成或验证实际 MSH。

## 小特征修复与 Edge intersects face 恢复

`surface-failure-retry-v5` 在网格前按命名归属将拓扑面分组，排除三个 interface
和进出口面，只在相同命名归属、相同部件内执行 Prime 0.5.1 的
`AutoQuadMesher.repair_topology` 与 `defeature_topology`。
前者连接容差内的面并清理边，后者进行部分去特征及细长面处理；
最小特征容差为当前最小网格尺寸的 15%，沿用导入 CAD 的长度单位。
这两个拓扑 API 在 0.5.1 中属于 Beta，仍需在 Prime 24.1 上验证效果。
处理后重新查询命名选择，避免继续引用旧拓扑 ID。

面网格改用 Lucid 的 `surface_mesh_with_size_controls()`，同时保留曲率、邻近、
叶轮/叶片/密封腔局部尺寸控制。随后在上述受限范围内执行 FREEFREE 缝合和
面片相交清理；不跨部件拼接，不处理三个 interface，也不使用全局外包面吞并内部接口。
Lucid 仍通过 Surfer 生成面网格，本身不保证修复几何；体网格仍由 AutoMesh 生成 POLY。

`EDGEINTERSECTINGFACEFOUND`（104）、AutoMesh 通用失败（100）、
明确的边面相交异常、`Surface meshing failed` 或未完成的面网格触发恢复；其他异常仍报告原始原因。
恢复时关闭当前 Prime、从原始 CAD 重新导入，将最小尺寸和整体尺寸同时放大 1.5 倍，并相应提高
去特征/缝合容差，局部尺寸不会小于该下限。最多 4 次几何恢复，
与最多 6 次成功网格后的单元数调节分别计数；调节单元数不会降低已提高的最小及整体尺寸下限。
恢复耗尽或仍无法达到 30–50 万单元时明确报告并返回状态 2，不导出未达标结果。

日志逐批打印输入面 ID、后端结果、实际移除/替换及新增的拓扑面/边 ID、
缝合面片 ID 与容差。拓扑 ID 变化表示拓扑被抑制或替换，不等同于删除了物理壁面；
后端未提供逐个小特征的类型及计数时不虚构统计。扩大容差会忽略更小的几何细节，
本机需复核密封间隙及实际网格质量。云端新增测试验证了相交失败后重新导入、
提高尺寸并成功导出的控制流程，不代表真实模型缺陷已修复。

面网格逐 part 调用 Lucid，失败时打印 part 名称/ID、本次提交的 topo face IDs、
后端可用的失败面 IDs 及逐面查询的未网格化面 IDs。后端不提供确切坏面时明确说明，
未网格化面清单用于定位，不等同于已确认的缺陷面。
