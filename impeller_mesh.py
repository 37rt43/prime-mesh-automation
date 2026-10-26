"""FFF-2 流体域自动 POLY 网格；Python 3.11 + PyPrimeMesh 0.5.x / Prime 24.1.

运行 python impeller_mesh.py --cad FFF-2.scdoc --out FFF-2.msh
直接使用模型已有的规范命名选择；缺失名称只警告，绝不自动识别边界。
"""
import argparse
from dataclasses import dataclass
import math
from pathlib import Path
import sys

import ansys.meshing.prime as prime
from main import launch_prime


SCRIPT_REVISION = "exact-names-label-presence-v3"
TARGET = 400_000
LOWER, UPPER = 300_000, 500_000
LAYERS, GROWTH = 5, 1.2
MAX_ATTEMPTS = 6
NAMED_SELECTIONS = (
    "inlet-section", "outlet-section", "interface-1", "interface-2", "interface-3",
    "inlet", "outlet", "impeller", "blade", "volute", "front-cover", "back-cover",
    "Seal_Chamber_Inlet", "Seal_Chamber_Outlet", "Seal_Chamber",
)
INTERFACES = ("interface-1", "interface-2", "interface-3")
REFINED_REGIONS = ("impeller", "blade", "Seal_Chamber_Inlet",
                   "Seal_Chamber_Outlet", "Seal_Chamber")
# Only these explicitly named surfaces grow prisms. Ports and interfaces never do.
WALL_NAMES = ("impeller", "blade", "volute", "front-cover", "back-cover", "Seal_Chamber")


@dataclass(frozen=True)
class Sizing:
    base: float
    minimum: float
    blade: float
    port: float
    first_height: float


def estimate_sizing(extents, features, scale=1.0):
    """BBox volume is only an initial estimate; measured cell counts close the loop.

    All lengths stay in imported CAD units; no mm/m assumption. The 0.25 fill
    factor is a heuristic, not a measured fluid volume. Curvature and proximity
    controls resolve small features beyond this initial estimate.
    """
    if any(not math.isfinite(x) or x <= 0 for x in extents):
        raise ValueError("流体域包围盒必须有三个非零、有限尺寸")
    length = math.sqrt(sum(x*x for x in extents))
    base = (0.25 * math.prod(extents) / TARGET) ** (1/3) * scale
    feature = min(features) if features else min(extents)
    # Scale feature caps too: adaptive coarsening must not get stuck at a cap.
    blade = min(base * 0.5, feature / 12 * scale)
    port = min(base * 0.7, feature / 8 * scale)
    minimum = min(blade * 0.3, length / 1500 * scale)
    total_layer_height = min(blade * 0.35, feature * 0.015 * scale)
    first = total_layer_height * (GROWTH-1) / (GROWTH**LAYERS-1)
    return Sizing(base, minimum, blade, port, first)


def checked(result, operation):
    """Lucid helpers sometimes discard server failures; check low-level results."""
    if hasattr(result, "error_code") and result.error_code != prime.ErrorCode.NOERROR:
        raise RuntimeError(f"{operation}: {result}")
    warnings = getattr(result, "warning_codes", None)
    if warnings is not None and len(warnings):
        print(f"{operation} 警告: {warnings}")
    return result


def bounds(model, zonelets):
    if not len(zonelets):
        raise RuntimeError("模型缺少可测量的几何/面 zonelets，不能估计包围盒")
    b = prime.SurfaceUtilities(model).get_bounding_box_of_zonelets(zonelets)
    low, high = (b.xmin, b.ymin, b.zmin), (b.xmax, b.ymax, b.zmax)
    return low, high, tuple(y-x for x, y in zip(low, high))


def geometry_zonelets(model, part, faces=None):
    if faces is None:
        faces = list(part.get_topo_faces())
        if not faces:
            return list(part.get_face_zonelets())
    return list(model.topo_data.get_geom_zonelets_of_topo_faces(faces))


def load_named_zones(model):
    """Exact, case-sensitive names only; no inference or closed-topology check.

    Reuse imported zones. CAD named selections imported as face labels are
    materialized under their original names. A named part scopes its surfaces
    for sizing only; no part/volume is renamed or merged.
    """
    groups = {name: {} for name in NAMED_SELECTIONS}
    params = prime.NamePatternParams(model)
    for name in NAMED_SELECTIONS:
        found = False
        for part in model.parts:
            topo = list(part.get_topo_faces())
            labels = list(part.get_labels())
            face_zone_ids = [z for z in part.get_face_zones() if model.get_zone_name(z) == name]
            volume_zone_ids = [z for z in part.get_volume_zones() if model.get_zone_name(z) == name]
            # Label existence is independent of whether Prime exposes any faces.
            # Body/volume labels and pre-mesh selections can legitimately be empty here.
            label_present = name in labels
            recognized = bool(label_present or face_zone_ids or volume_zone_ids or part.name == name)
            found |= recognized
            faces, zonelets = set(), set()
            if label_present:
                if topo:
                    faces.update(map(int, part.get_topo_faces_of_label_name_pattern(name, params)))
                else:
                    zonelets.update(map(int, part.get_face_zonelets_of_label_name_pattern(name, params)))
            if face_zone_ids:
                if topo:
                    faces.update(map(int, part.get_topo_faces_of_zone_name_pattern(name, params)))
                else:
                    zonelets.update(map(int, part.get_face_zonelets_of_zone_name_pattern(name, params)))
            part_region = part.name == name or bool(volume_zone_ids)
            if part_region:
                # A named volume scopes its boundary, rather than becoming a face zone.
                if volume_zone_ids:
                    volumes = part.get_volumes_of_zone_name_pattern(name, params)
                    boundary = list(part.get_face_zonelets_of_volumes(volumes)) if len(volumes) else []
                    if topo:
                        faces.update(f for f in topo if set(
                            model.topo_data.get_mesh_zonelets_of_topo_faces([f])) & set(boundary))
                    else:
                        zonelets.update(boundary)
                if part.name == name or (volume_zone_ids and len(part.get_volume_zones()) == 1):
                    faces.update(topo)
                    if not topo:
                        zonelets.update(map(int, part.get_face_zonelets()))
            groups[name][part.id] = dict(faces=sorted(faces), zonelets=sorted(zonelets),
                face_zones=face_zone_ids, region=part_region, recognized=recognized)
            if recognized and not (faces or zonelets):
                print(f"警告: 已识别 '{name}'（部件 {part.name}），但暂无可用面实体；保留名称并继续")
        if not found:
            print(f"警告: 未找到命名选择/zone '{name}'，跳过该名称并继续")
    return groups


def prepare_named_zones(model, groups):
    """Keep every interface separately; never merge, stitch, connect or relabel it as wall."""
    for name in (*[n for n in NAMED_SELECTIONS if n not in INTERFACES], *INTERFACES):
        per_part = groups[name]
        zone_id = next((z for item in per_part.values() for z in item['face_zones']), None)
        for part in model.parts:
            item = per_part[part.id]
            if not item['region'] and (item['faces'] or item['zonelets']):
                if zone_id is None:
                    zone_id = checked(model.create_zone(name, prime.ZoneType.FACE),
                                      f"建立 {name} zone").zone_id
                if item['faces']:
                    checked(part.add_topo_entities_to_zone(zone_id, item['faces']), f"保留 {name}")
                else:
                    checked(part.add_zonelets_to_zone(zone_id, item['zonelets']), f"保留 {name}")
    # Interfaces and port faces take priority over broad region selections.
    for part in model.parts:
        blocked_faces, blocked_zonelets = set(), set()
        for name in (*INTERFACES, "inlet", "outlet", "Seal_Chamber_Inlet", "Seal_Chamber_Outlet"):
            item = groups[name][part.id]
            blocked_faces.update(item['faces'])
            blocked_zonelets.update(item['zonelets'])
        for name in NAMED_SELECTIONS:
            item = groups[name][part.id]
            if name in (*REFINED_REGIONS, "inlet", "outlet"):
                if item['faces']:
                    checked(part.add_labels_on_topo_entities([f"mesh_refine_{name}"], item['faces']), "局部尺寸标签")
                if item['zonelets']:
                    checked(part.add_labels_on_zonelets([f"mesh_refine_{name}"], item['zonelets']), "局部尺寸标签")
            if name in WALL_NAMES:
                faces = sorted(set(item['faces']) - blocked_faces)
                zonelets = sorted(set(item['zonelets']) - blocked_zonelets)
                if faces:
                    checked(part.add_labels_on_topo_entities(["mesh_prism_wall"], faces), "壁面层标签")
                if zonelets:
                    checked(part.add_labels_on_zonelets(["mesh_prism_wall"], zonelets), "壁面层标签")


def generate(model, sizing, groups):
    model.set_global_sizing_params(prime.GlobalSizingParams(
        model, min=sizing.minimum, max=sizing.base, growth_rate=GROWTH))
    controls = []
    for kind in (prime.SizingType.CURVATURE, prime.SizingType.PROXIMITY):
        c = model.control_data.create_size_control(kind)
        checked(c.set_scope(prime.ScopeDefinition(model, label_expression="*",
            entity_type=prime.ScopeEntity.FACEZONELETS,
            evaluation_type=prime.ScopeEvaluationType.LABELS)), "全局尺寸范围")
        common = dict(min=sizing.minimum, max=sizing.base, growth_rate=GROWTH)
        if kind == prime.SizingType.CURVATURE:
            c.set_curvature_sizing_params(prime.CurvatureSizingParams(
                model, **common, normal_angle=15, use_cad_curvature=True))
        else:
            c.set_proximity_sizing_params(prime.ProximitySizingParams(
                model, **common, elements_per_gap=4,
                ignore_self_proximity=False, ignore_orientation=True))
        controls.append(c.id)
    for name in (*REFINED_REGIONS, "inlet", "outlet"):
        if not any(item["faces"] or item["zonelets"] for item in groups[name].values()):
            continue
        c = model.control_data.create_size_control(prime.SizingType.SOFT)
        checked(c.set_scope(prime.ScopeDefinition(model, label_expression=f"mesh_refine_{name}")),
                f"{name} 尺寸范围")
        c.set_soft_sizing_params(prime.SoftSizingParams(model,
            max=sizing.blade if name in REFINED_REGIONS else sizing.port, growth_rate=GROWTH))
        controls.append(c.id)
    sf = checked(prime.SizeField(model).compute_volumetric(controls,
        prime.VolumetricSizeFieldComputeParams(model)), "尺寸场")
    for part in model.parts:
        topo_faces = list(part.get_topo_faces())
        surfer_params = prime.SurferParams(model, size_field_type=prime.SizeFieldType.VOLUMETRIC,
            min_size=sizing.minimum, max_size=sizing.base, growth_rate=GROWTH)
        if topo_faces:
            checked(prime.Surfer(model).mesh_topo_faces(part.id, topo_faces, surfer_params),
                    f"{part.name} 面网格")
        else:
            checked(prime.Surfer(model).remesh_face_zonelets(part.id,
                part.get_face_zonelets(), part.get_edge_zonelets(), surfer_params),
                f"{part.name} 面网格")
        summary = part.get_summary(prime.PartSummaryParams(model))
        if summary.n_unmeshed_topo_faces or not summary.n_faces:
            raise RuntimeError(f"{part.name}: 存在未划分的面")
        # Reconstruct volumes only when import has none; do not merge any interfaces.
        if not len(part.get_topo_volumes()) and not len(part.get_volumes()):
            params = prime.ComputeVolumesParams(model)
            if topo_faces:
                checked(part.compute_topo_volumes(params), "计算体积")
            else:
                checked(part.compute_closed_volumes(params), "计算体积")
        # Preserve all imported volume names; only create a fallback when absent.
        if not len(part.get_volume_zones()):
            fluid = checked(model.create_zone(part.name if part.name in NAMED_SELECTIONS else f"fluid_{part.id}",
                                              prime.ZoneType.VOLUME), "流体 zone")
            if len(part.get_topo_volumes()):
                checked(part.add_topo_entities_to_zone(fluid.zone_id, part.get_topo_volumes()), "流体体积")
            else:
                checked(part.add_volumes_to_zone(fluid.zone_id, part.get_volumes()), "流体体积")
        vc = model.control_data.create_volume_control()
        vc.set_params(prime.VolumeControlParams(model, cell_zonelet_type=prime.CellZoneletType.FLUID))
        checked(vc.set_scope(prime.ScopeDefinition(model,
            entity_type=prime.ScopeEntity.VOLUME, evaluation_type=prime.ScopeEvaluationType.ZONES,
            part_expression=part.name, zone_expression="*")), "流体范围")
        prism_ids = []
        if "mesh_prism_wall" in part.get_labels():
            pc = model.control_data.create_prism_control()
            checked(pc.set_surface_scope(prime.ScopeDefinition(model,
                part_expression=part.name, label_expression="mesh_prism_wall")), "边界层壁面")
            checked(pc.set_volume_scope(prime.ScopeDefinition(model,
                entity_type=prime.ScopeEntity.VOLUME, evaluation_type=prime.ScopeEvaluationType.ZONES,
                part_expression=part.name, zone_expression="*")), "边界层体积")
            pc.set_growth_params(prime.PrismControlGrowthParams(model,
                offset_type=prime.PrismControlOffsetType.UNIFORM, n_layers=LAYERS,
                growth_rate=GROWTH, first_height=sizing.first_height))
            prism_ids.append(pc.id)
        checked(prime.AutoMesh(model).mesh(part.id, prime.AutoMeshParams(model,
            volume_fill_type=prime.VolumeFillType.POLY, max_size=sizing.base,
            size_field_type=prime.SizeFieldType.VOLUMETRIC,
            volume_control_ids=[vc.id], prism_control_ids=prism_ids)), f"{part.name} POLY 体网格")
    # Keep field active through volume meshing, then release controls.
    model.delete_volumetric_size_fields([sf.size_field_id])
    summaries = [p.get_summary(prime.PartSummaryParams(model)) for p in model.parts]
    if any(s.n_cells <= 0 or s.n_poly_cells <= 0 for s in summaries):
        raise RuntimeError("部分流体域缺少体单元或 POLY 单元")
    if not any(s.n_prism_cells > 0 for s in summaries):
        print("警告: 未生成棱柱边界层，请检查已命名壁面范围和后端日志")
    return sum(s.n_cells for s in summaries)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cad", type=Path, default=Path("FFF-2.scdoc"))
    p.add_argument("--out", type=Path, default=Path("FFF-2.msh"))
    return p.parse_args()


def main():
    args = parse_args()
    print(f"impeller_mesh 版本: {SCRIPT_REVISION}; 脚本路径: {Path(__file__).resolve()}")
    if not prime.__version__.startswith("0.5.") or not (3, 8) <= sys.version_info[:2] <= (3, 11):
        raise RuntimeError("必须使用 Python 3.8–3.11 与 PyPrimeMesh 0.5.x / Prime 24.1")
    if not args.cad.is_file():
        raise FileNotFoundError(f"未找到模型: {args.cad.resolve()}")
    if args.out.suffix.lower() != ".msh":
        raise ValueError("输出必须使用 .msh 扩展名")
    scale = 1.0
    last_count = None
    for attempt in range(1, MAX_ATTEMPTS+1):
        # Fresh Prime model each iteration avoids stale zones, mesh and controls.
        # launch_prime() is centralized in main.py, using its secure default.
        with launch_prime() as client:
            model = client.model
            checked(prime.FileIO(model).import_cad(str(args.cad.resolve()),
                prime.ImportCadParams(model, geometry_transfer=True)), "读取 CAD")
            if not model.parts:
                raise RuntimeError("CAD 未导入任何部件")
            groups = load_named_zones(model)
            zonelets = [int(z) for p in model.parts for z in geometry_zonelets(model, p)]
            low, high, extents = bounds(model, zonelets)
            print(f"包围盒 min={low}, max={high}; 整体尺寸={extents}（CAD 导入单位）")
            features = []
            for name in (*REFINED_REGIONS, "inlet", "outlet"):
                ids = []
                for part in model.parts:
                    item = groups[name][part.id]
                    ids.extend(geometry_zonelets(model, part, item['faces'])
                               if item['faces'] else item['zonelets'])
                if ids:
                    _, _, dims = bounds(model, ids)
                    features.extend(d for d in dims if d > max(extents)*1e-8)
            sizing = estimate_sizing(extents, features, scale)
            print(f"第 {attempt}/{MAX_ATTEMPTS} 次: {sizing}; 层数={LAYERS}")
            prepare_named_zones(model, groups)
            last_count = generate(model, sizing, groups)
            print(f"实际体网格总数（含边界层）: {last_count}")
            if LOWER <= last_count <= UPPER:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                checked(prime.FileIO(model).export_fluent_meshing_mesh(str(args.out.resolve()),
                    prime.ExportFluentMeshingMeshParams(model)), "导出 Fluent MSH")
                if not args.out.is_file() or args.out.stat().st_size == 0:
                    raise RuntimeError("导出未产生有效文件")
                print(f"已导出: {args.out.resolve()}")
                print(f"实际生成的体网格总数: {last_count}")
                return
            # N ~ h^-3; damping reduces overshoot with surface/prism contributions.
            scale *= max(0.65, min(1.6, (last_count / TARGET) ** (0.8/3)))
    raise RuntimeError(f"{MAX_ATTEMPTS} 次后仍未达到 30–50 万: {last_count}；未导出")


if __name__ == "__main__":
    main()
