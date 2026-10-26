# AGENTS.md —— 给 Codex / AI 编程助手的项目说明

## 项目目标
使用 PyPrimeMesh（`ansys-meshing-prime`，GitHub: ansys/pyprimemesh）通过 Python 调用
Ansys Prime Server 完成网格划分（面网格、体网格、边界层、多面体/六面体核心、导入导出），
替代手工 Meshing/Fluent Meshing 操作。

## 版本兼容（最高优先级，务必遵守）
- 本机后端固定为 **Ansys 2024R1，Prime Server 24.1.0**。
- PyPrimeMesh 客户端与 Prime Server **一对一兼容**，24.1.0 对应客户端 **0.5.x**。
  禁止使用 0.6 及以上客户端（0.6→24.2、0.7→25.1、0.9→25.2、0.10→26.1），
  版本不匹配会连不上服务。
- 0.5.x 仅支持 **Python 3.8–3.11**，虚拟环境必须用 **Python 3.11**，不要用 3.12+。
- 依赖见 `requirements.txt`（已固定 `ansys-meshing-prime==0.5.*`）。

## 连接方式
- 本机 Windows：`import ansys.meshing.prime as prime` →
  `with prime.launch_prime() as prime_client:`（默认安全连接，Windows 用 interceptor 校验）。
- 不要使用 insecure 连接，除非容器明确以 insecure 模式启动。
- 云端 Codex 沙箱内没有 Prime Server 和许可证，代码只能做静态检查；
  真正划分网格必须在本机连接 24.1 后端。
- 禁止使用 Linux Docker 方案（Docker 镜像从 2025R1 起，且需 NUMA，本机不满足）。

## 常用 API（0.5.x）
- 启动：`prime.launch_prime()`；模型：`prime_client.model`
- 高层接口（优先使用）：`prime.lucid.Mesh(model=model)`
  - `mesh_util.read(file_name=...)` 导入 CAD
  - `mesh_util.surface_mesh(min_size=..., max_size=...)`
  - `mesh_util.volume_mesh(volume_fill_type=prime.VolumeFillType.POLY)`
  - `mesh_util.write(file_name=...)`
- 底层接口：`model.parts`、`prime.SizeControl`、`prime.PrismControl`、`prime.BoundaryLayerControl`
- 写代码前先确认该 API 在 0.5.x 中存在，不要照抄高版本文档的方法签名。

## 许可证
Prime Server 按顺序检出：CFD PrepPost、CFD PrepPost Pro、Mechanical Enterprise PrepPost、
Mechanical Enterprise、Mechanical Pro、Mechanical Premium、LS-DYNA。

## 代码约定
- 连接逻辑统一放在 `main.py`，用 `with prime.launch_prime()` 保证退出时释放。
- 网格尺寸、边界层参数集中放在参数/配置区，便于调整。
- 不硬编码许可证服务器与文件绝对路径。

## 验证
- 不依赖后端：`python -m py_compile main.py`
- 连接验证：`python main.py`（应打印 0.5.x 版本和服务器 PID）
- 网格验证：`python main.py --cad <文件.step> --out meshed.msh`
