# FFF-8 本机网格自动化交付说明

适用：Windows、Python **3.11**、Ansys **2024 R1 / Prime Server 24.1**、PyPrimeMesh **0.5.1**。
云端没有启动 Prime，没有导入 CAD，没有划分网格，没有生成任何网格质量实测报告。
当前仓库没有 FFF-8.scdoc，需将交付目录与本机 CAD 放在一起运行。

## 先说明范围

这是经过静态检查的本机执行脚本，**不能宣称已完整实现或验证用户清单中所有高级诊断**。
以下能力在此交付中尚未实现，不能以部件统计代替它们：

- 精确坏体单元 ID、坐标，以及隔舌、叶片前后缘、密封间隙和 interface 邻域的独立体单元质量统计。
  0.5.1 的 `VolumeSearch.get_volume_quality_summary` 只提供部件级极值和失败数量。
- 密封真实最小间隙和 CAD 曲率半径的显式数值提取。脚本记录几何边包围盒跨度代理值，
  **不会把它当成真实间隙**；实际曲率和 proximity 尺寸由 Prime 尺寸场计算。
- 隔舌和叶片前后缘的独立几何语义识别。当前保守加密整个命名壁面，并叠加曲率和 proximity 控制。
- 实际膨胀层逐层厚度、壁面覆盖率、塌缩/折叠的专门检测。报告区分 requested 参数与 actual=null。
- Fluent 读回后逐项核对命名、边界类型以及 Fluent 质量定义的独立验收。导出前只检查 Prime 中的命名。

故成功状态明确是 `part_quality_passed_with_documented_diagnostic_limitations`，不是全清单验收通过。
如项目必须逐项通过全部原始验收要求，需要补充本机 Fluent 诊断模块和真实模型验证。

## 文件

- pump_mesh_auto.py：完整主工作流；所有长度由导入几何推导，没有绝对毫米尺寸或目标网格数量。
- main.py：集中、安全的本机 `launch_prime` 入口，导入模块不会启动服务器。
- mesh_config.json：仅相对比例、阈值、无量纲参数；可选 Fluent 可执行文件路径。
- requirements.txt：固定客户端与 Python 依赖；Ansys 软件及许可证不通过 pip 安装。
- STATIC_CHECKS.txt：本次静态检查记录，不是网格质量报告。
- verify_static.py：可复核 API 名称/参数的静态检查脚本，不调用 Prime 服务。

## 本机运行（PowerShell）

将压缩包解压到独立目录，保留 main.py 与 pump_mesh_auto.py 同级。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python pump_mesh_auto.py --cad ./FFF-8.scdoc --output ./output_check --dry-run
python pump_mesh_auto.py --cad ./FFF-8.scdoc --output ./output_mesh --config ./mesh_config.json --target-orthogonal-quality 0.1 --max-iterations 4 --mesh-format Fluent --report ./output_mesh/mesh_quality_report.json
```

cmd.exe 激活命令：`.\.venv\Scripts\activate.bat`。
`--dry-run` 会在**本机启动 Prime 并导入 CAD**，仅检查几何与命名，不划分面/体网格，不输出最终网格。
`--help` 不导入 Prime 模块，也不启动服务器。

本机安装应能由 Prime 客户端自动定位 2024 R1 安装。
若系统安装多个 Ansys 版本，先确保本次环境只定位 24.1 后端；脚本不设置 insecure 连接。
无需在配置中写许可证服务器。

## HDF5 网格输出需要本机 Fluent

0.5.1 原生文档路径支持 Fluent Meshing `.msh` 以及 Fluent `.cas.h5`，
没有可核实的 `.msh.h5` 导出参数。因此脚本先导出 `pump_intermediate.msh`，
然后使用**本机 Fluent 2024 R1**批处理转换为真正的 `pump.msh.h5`。
这额外需要本机 Fluent 可运行及相应许可证，不能只安装 PyPrimeMesh。
自动查找顺序：配置 `fluent_executable` → PATH 中的 fluent → `%AWP_ROOT241%` 下的 Fluent。
若自动查找失败，编辑配置，例如：

```json
{"fluent_executable": "C:/Program Files/ANSYS Inc/v241/fluent/ntbin/win64/fluent.exe"}
```

脚本生成转换 journal、保留转换日志，检查子进程退出码、真实 HDF5 格式和 `meshes` 组。
不会将 `.msh` 改后缀伪装为 HDF5。TUI journal 必须在用户本机 2024 R1 验证；若命令提示或转换失败，
脚本返回失败并保留原始 `.msh`，不会宣称 `.msh.h5` 成功。

## 命名与分区

四组区域名称使用精确匹配，区域显示名/别名接受 Inlet、Impeller、Volute-Reargap-Outlet、Seal_Chamber；
清单中明确列出的四个 body 名仍必须存在。所有列出的边界名必须具有非空 CAD 面范围。
缺失或空范围均抛出异常，并在日志列出实际 part/label/zone 名；不做模糊匹配、不静默重命名。
原始名称在每次导入后记录，在导出前复核；CAD 标签需要导出为 zone 时使用原名创建。
额外原始标签保留；没有显式壁面/端口/interface 分类的面将报错，不能猜测它属于 wall。
要求每个导入 part 恰属于一个流体区域，且四区域都可解析；额外固体或混合区域会明确拒绝。
若实际 CAD 的组织方式不满足这些条件，需要针对其真实拓扑扩展区域解析，不能直接误划为流体。

真实壁面白名单：impeller_blade_wall、impeller_front_cover、impeller_rear_cover、sealchamber-end、
seal_chamber_wall、inlet_wall、volute_wall。
sealchamber_inlet、inlet、outlet 和四个 interface 均排除膨胀层。
若某个面的壁面/端口/interface 分类重叠将报错。默认不连接、不合并两个 interface 面。
zone 名称不等同于 Fluent 已设置的物理边界条件，使用前须在 Fluent 中检查。

## 自动尺寸与边界层

整体基准尺寸 = 整体 bounding box 对角线 × global_diagonal_ratio。
局部特征代理值 = 各部件几何边包围盒对角线分布的低分位跨度；局部尺寸受该跨度和整体尺寸相对比例共同限制。
平滑增长率根据最大/最小尺寸比和过渡步数推导，并受配置上限限制。
所有壁面、端口和 interface 有命名范围尺寸控制；CAD curvature 和 proximity 叠加生成体尺寸场。
比例、模型尺度、各部件尺寸、代理值、计算出的尺寸和请求层参数全部记录到日志及 JSON。
包围盒跨度不是几何体积、真实壁面间距或水力直径。

膨胀层首层高度依据局部尺寸比例，层数由允许总厚度预算与几何级数反算，增长率由局部尺度比推导。
未使用流量、转速、介质参数，不宣称达到某个 y+。
显式开启 `PrismStairStep(check_proximity=True, gap_factor_scale=0.5)`，让后端处理局部近邻碰撞；
此行为不能替代对窄间隙实际层数/厚度的本机验收。请求参数可能被后端局部截断。
若要求层的部件完全没有棱柱单元，脚本立即失败。

## 质量与修复

各 part 的 INVERSEORTHOGONAL、SKEWNESS、ASPECTRATIO、ELEMENTQUALITY 均必须返回有限统计。
脚本使用 `正交质量 = 1 - 最大 INVERSEORTHOGONAL`，且要求严格大于指定目标（目标不得低于 0.1）。
这不是未经比对的 Fluent 验收保证。其余指标使用配置阈值；边界层高 aspect ratio 可能需要物理上合理的项目阈值。

失败先请求内部节点移动修复并重新统计；仍失败时重新启动 Prime、导入原始 CAD、缩小失败部件尺寸和请求首层高度，
重建表面和体尺寸场、重新划分整个模型。不会复用被部分失败操作修改的模型。
蜗壳优先 POLY，可配置 HEXCOREPOLY；检查枚举能力。非 TET 模式网格操作失败时明确记录原因，
在下一次允许的迭代使用新模型回退 TET。失败原因不会被描述成已证实的 API 不支持。
超过迭代次数仍不达标返回 2，不导出最终网格。
`--max-iterations` 是初始尝试之外的最多重划次数，默认 4，即最多 5 次。
另对各命名边界统计**表面面片质量**，明确标识不是邻近体单元统计。

## 输出与失败行为

成功导出：pump.msh.h5、mesh_quality_report.json、mesh_quality_summary.csv、meshing_log.txt、
zone_and_boundary_check.csv，并保留原生网格及转换 journal/log。
可用 `--report` 改 JSON 路径，CSV 和日志仍放在 `--output`。
缺 CAD/命名失败/后端失败/质量不合格/转换失败均返回 2，并尽可能保留日志、JSON、CSV；
CAD 尚未导入时自然无法生成真实 zone 清单，JSON 不会伪造该清单。
输出目录已有 pump.msh.h5 时拒绝覆盖，避免旧成功网格被误认为本次结果。
重新执行请选新目录。dry-run 返回成功只表示几何与命名检查通过。

## API 对照来源

实际核对 PyPI 官方发布的 ansys-meshing-prime==0.5.1 wheel 的 autogen/core/lucid 源码及签名：
https://pypi.org/project/ansys-meshing-prime/0.5.1/
源码项目：https://github.com/ansys/pyprimemesh
重点包括 automeshstructs.py、volumesearchstructs.py、volumemeshtool.py、prismcontrolstructs.py、
fileiostructs.py、lucid/mesh_util.py。没有照抄最新版本 API。

**网格数量、最小正交质量、skewness、实际边界层厚度均为“待本机验证”。**
