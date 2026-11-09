# FFF-8 泵流体网格自动化（Prime 2024 R1）

环境：Windows、Python **3.11**、Ansys Prime Server **24.1**、PyPrimeMesh **0.5.1**。
云端只执行离线检查，不启动 Prime，不导入 CAD，不生成计算网格。

## 运行

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python pump_mesh_auto.py --cad ./FFF-8.scdoc --output ./recognition_check --dry-run
python pump_mesh_auto.py --cad ./FFF-8.scdoc --output ./mesh_run --config ./mesh_config.json --target-orthogonal-quality 0.1 --max-iterations 4
```

cmd.exe 激活使用 `.\.venv\Scripts\activate.bat`。
每次使用新的输出目录，避免把旧网格当成本次结果。
`--dry-run` 会在**本机**启动 Prime，导入几何并执行自动识别，写映射报告，但不划分面/体网格。
`--help` 不启动 Prime。`main.py` 统一提供安全启动入口；不使用 insecure 连接或 Docker。
CAD 文件需要用户提供，仓库不含 `FFF-8.scdoc`。

支持参数：`--cad`、`--output`、`--target-orthogonal-quality`、`--max-iterations`、
`--mesh-format Fluent`、`--dry-run`、`--report`、`--config`。
`--report` 指定 JSON 路径，CSV/日志仍位于 `--output`。

## 区域识别：不再要求原始命名

CAD 使用 `PartCreationType.BODY` 导入，尽量令一个 CAD body 对应一个 part。
不再要求 Inlet、inlet_body、原始 wall/interface 等命名选择存在；`label0:xxx`、空 face_zones 均可处理。
原始 label 和 topo face ID 只用来追溯，不作为必须满足的检查条件。

- volume zone 或 part 明确命名为 `solid` 的 body 从工作模型删除，不参与网格、尺寸场、质量统计和导出。
- `impeller`/`rotor`、`volute`、`stator`/`stationary`、`seal` 等可选名称提示参与区域判断。
  因此 ns_impeller_1_flowdomainvirtual、ns_volute_1_flowdomainvirtual、ns_stator_1_flowdomain 可分别识别。
- 名称发生变化时，使用 body 主方向跨度、扁平/近圆特征和接触图判断旋转区、邻接蜗壳候选、静止区和密封区。
  几何推断的区域标记 `uncertain`。没有固定要求“四个区域必须全部存在”。
- 没有材料证据的未知 body 暂保留为不确定流体候选。**仅凭几何无法唯一证明材料是流体还是固体**，
  请在本机检查区域报告；不能声称能可靠识别任意改名后的实体材料。

## 面边界识别顺序

1. **先 interface**：读取 CAD 显示面片的顶点/连接关系，检测跨 body 的候选接触。
   包围盒仅用于初筛，随后计算点到三角片的距离和面覆盖率，避免仅凭包围盒相交就认定接触。
   使用面积加权的确定性采样，同时探测面片顶点；不同 CAD 面分割可通过对侧候选面的并集检测。
   两侧分别命名 `auto_interface_1`、`auto_interface_2` 等，`interface_pair` 列关联对应面。
   **不缝合、不合并接口两侧**，也不自动建立 Fluent 的 mesh-interface 配对。
2. **再端口**：从未分配面中筛选近似平面、圆形/环形、位于 body 法向极端位置、相邻壁面沿轴向延伸的候选面。
   使用旋转区轴向和静止区偏好选 `auto_inlet`，使用蜗壳偏好选 `auto_outlet`。
   几何无法唯一确定流向，因此端口也标记 `uncertain`；无可靠候选时不强造端口，不中断。
3. **最后所有剩余面归 wall**：`auto_impeller_wall`、`auto_volute_wall`、`auto_stationary_wall`、`auto_seal_wall`。
   未知、缺少显示面片、不能确认接触、其他额外端面均默认 wall，并写明 `uncertain` 和识别依据。
   不存在“名称不匹配即退出”或“未分类面不分配”的路径。

面积、法向、中心和形状指标来自 **CAD 显示用离散面片**，不等于生成了计算网格，也不是精确 CAD 积分面积。
面积单位为 CAD 导入长度单位的平方；无法获取面片时面积留空，不能编造。
显示面片不足、曲面两侧离散误差、仅部分重合且需要拓扑切分、分割端盖，均可能降低识别准确性。
不能完整确认的面按要求回退 uncertain wall；这类结果必须在 Fluent 中检查，不能直接视为可求解模型。

识别参数集中在 `mesh_config.json`：

- recognition_contact_ratio：接触容差/整体 bounding box 对角线。
- recognition_contact_coverage：判定整个面为接触面的最低采样覆盖率。
- recognition_planarity_ratio：平面偏差与端部位置判断的相对容差。
- recognition_samples：每面最多使用的面积分层采样数（还会检查对应三角片顶点）。

没有写死任何绝对网格长度或目标网格数量。调整识别容差时注意真实密封间隙，过大容差可能把近邻壁面误判为接触。

## 自动网格与质量

所有自动分区参与非结构化网格。整体尺寸由 bounding box 对角线比例推导，局部尺寸使用几何边跨度代理值。
自动 wall、端口和 interface 均有局部尺寸控制，并叠加 CAD curvature/proximity sizing 和平滑增长率。
叶片、隔舌、弯道、密封等特征通过所在壁面整体加密和曲率/近邻控制覆盖，**不宣称已独立语义定位每个前后缘或隔舌**。

棱柱层只作用于自动 wall 子集；旧标签即使含有同名 `__pump_wall` 也先清空再按自动分类重建，避免端口/interface 混入。
首层高度、层数、增长率由局部尺度和预算推导；启用 `PrismStairStep` proximity 碰撞处理。
实际壁面身份存在不确定性时，按用户指定的 wall 回退原则处理，不能保证该面物理上一定是壁面。
无流动参数时不声称达到目标 y+。请求层参数和实际厚度分别记录，实际厚度未测量时为 null。

蜗壳默认 POLY，可选 HEXCOREPOLY；客户端检查枚举能力。不支持或网格操作失败时，在新的模型/迭代中回退 TET。
各部件检查 INVERSEORTHOGONAL、SKEWNESS、ASPECTRATIO、ELEMENTQUALITY。
`最小正交质量 = 1 - 最大 INVERSEORTHOGONAL`，要求严格大于目标，目标不得低于 0.1。
先尝试节点移动修复，再重新导入原 CAD、重新识别、缩小失败部件尺寸及请求首层高度、重建表面和体网格。
`--max-iterations` 为初始尝试外的最多重划次数，默认 4。

**识别不确定性不会中断网格流程；CAD 无有效体、服务器操作失败、质量耗尽仍不合格等真实错误仍会报错。**
不会为了保证“输出成功”而绕过质量阈值。
0.5.1 的质量 API 提供部件级统计，没有精确坏体单元坐标，也没有关键特征邻域的独立体质量统计。
命名面的表面质量另行记录，不冒充邻近体单元质量。

## 输出

- `auto_boundary_mapping.csv`：每个流体 topo face 一行，包括原始 label/ID、新 zone 名、预期类型、区域、
  face_count、面积/面积状态、中心、法向、平面性、法向一致性、uncertain、识别依据和 interface_pair。
- `meshing_log.txt`：完整操作、假设、尺寸、警告与失败原因。
- `mesh_quality_report.json`：原始名字快照、自动区域/排除实体、识别映射、每次网格参数与质量统计。
- `mesh_quality_summary.csv`：部件级质量摘要。
- `pump.msh`：质量达标后导出的原生 Fluent 可读网格。
- 可选 `pump.msh.h5`：本机 Fluent 可用时，执行 journal 转换并验证真实 HDF5 格式及 meshes 组。

Fluent 查找顺序：配置 fluent_executable → PATH 中 fluent → `%AWP_ROOT241%` 下 Fluent 2024 R1。
HDF5 转换还需要本机 Fluent 许可证。转换不可用/失败时保留有效原生 `pump.msh`，记录警告并交付该文件。
不会简单改后缀伪装 HDF5。

映射中的 zone_type 是**识别出的预期物理类型**。Prime 的 FACE zone 不是 Fluent 求解器完整边界条件配置。
原生 `.msh` 请按 CSV 在 Fluent 检查/设置类型；可选转换 journal 会请求 velocity-inlet、pressure-outlet、
interface、wall 类型，但仍需本机验证，接口配对和物理工况由用户配置。

## 离线验证

```powershell
python -m py_compile pump_mesh_auto.py main.py verify_static.py
python verify_static.py
python -m unittest discover -s tests -v
```

测试使用解析几何和模拟模型，**不启动 Prime、不读取真实 CAD、不划分网格**。
覆盖接触优先级、分割接触、非接触重叠包围盒、真实间隙、部分接触回退、圆环面积、端部识别、
旋转/单位缩放、区域改名、solid 排除、通用 label 追溯、缺少面片仍全分配，以及旧 wall 标签不污染端口/interface。
静态核对使用官方 PyPI ansys-meshing-prime==0.5.1 源码及签名：
https://pypi.org/project/ansys-meshing-prime/0.5.1/

**网格数量、最小正交质量、skewness、实际膨胀层厚度及真实 CAD 识别效果，均待本机验证。**
