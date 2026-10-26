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
体填充为 POLY，边界层使用棱柱。逐部件检查存在 POLY、棱柱及非零体单元。

每轮打印实际总单元数（包含边界层）；按实际数量自动调整尺寸并重新导入网格，
最多尝试 6 轮，仅在总数处于 300000–500000 时导出 `FFF-2.msh`。
若未达标、导入/网格/导出失败，抛出异常并以非零状态退出，不导出未达标结果。
旧有输出文件不会在失败时被删除，请以本次成功日志为准。

模型必须是已封闭的**流体域**，脚本不会从泵实体中自动提取流体，也不自动封口。
多部件需在 CAD 中预先正确共享拓扑；脚本不创建转静接口。
根据已有 CAD 面标签匹配 `inlet/进口/入口`、`outlet/出口`、`blade/叶片`、
`volute/蜗壳`，建立 `inlet`、`outlet`、`blades`、`volute` 面 zone，
剩余面归入 `wall_other`，每个流体部件建立 `fluid_<part_id>` 体 zone。
这些是命名分区，Fluent 求解边界条件仍需设置。
缺少语义标签或分类重叠会停止并打印标签清单，不凭几何猜测物理边界。
若已有标签名称不同，可创建映射文件，例如：

```json
{
  "inlet": ["suction*"],
  "outlet": ["discharge*"],
  "blades": ["vane*"],
  "volute": ["casing*"]
}
```

```powershell
python impeller_mesh.py --zones-json zones.json
python -m py_compile main.py impeller_mesh.py
python -m unittest discover -s tests -v
```

本仓库云端验证仅覆盖脚本语法、0.5.1 API 签名及无后端逻辑。
未提供 `FFF-2.scdoc`，且云端没有 Prime Server/许可证，因此尚未生成或验证实际 MSH。
