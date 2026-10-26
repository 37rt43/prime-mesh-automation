"""FFF-2 流体域自动 POLY 网格；Python 3.11 + PyPrimeMesh 0.5.x / Prime 24.1.

运行 python impeller_mesh.py --cad FFF-2.scdoc --out FFF-2.msh
CAD 必须是已封闭的流体域，并包含可识别的进口、出口、叶片、蜗壳面标签。
无语义标签时用 --zones-json 指定已有标签的 glob 模式；不猜测物理边界。
"""
import argparse
from dataclasses import dataclass
import fnmatch
import json
import math
from pathlib import Path
import sys

import ansys.meshing.prime as prime
from main import launch_prime


TARGET = 400_000
LOWER, UPPER = 300_000, 500_000
LAYERS, GROWTH = 5, 1.2
MAX_ATTEMPTS = 6
DEFAULT_PATTERNS = {
    "inlet": ["*inlet*", "*进口*", "*入口*"],
    "outlet": ["*outlet*", "*出口*"],
    "blades": ["*blade*", "*叶片*"],
    "volute": ["*volute*", "*蜗壳*"],
}


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


def face_entities(model, part):
    faces = list(part.get_topo_faces())
    return faces, list(model.topo_data.get_geom_zonelets_of_topo_faces(faces))


def bounds(model, faces):
    zonelets = model.topo_data.get_geom_zonelets_of_topo_faces(faces)
    if not len(zonelets):
        raise RuntimeError("CAD 缺少几何面 zonelets，不能估计包围盒")
    b = prime.SurfaceUtilities(model).get_bounding_box_of_zonelets(zonelets)
    low, high = (b.xmin, b.ymin, b.zmin), (b.xmax, b.ymax, b.zmax)
    return low, high, tuple(y-x for x, y in zip(low, high))


def identify_boundaries(model, patterns):
    """Return disjoint face sets; preserve all unmatched surfaces as walls."""
    groups = {name: {} for name in patterns}
    inventory = {}
    for part in model.parts:
        faces, _ = face_entities(model, part)
        if not faces or not len(part.get_topo_volumes()):
            raise RuntimeError(f"{part.name}: 需要封闭的 CAD 流体体积及拓扑面")
        labels = list(part.get_labels())
        inventory[part.name] = labels
        claimed = set()
        for name, globs in patterns.items():
            selected = set()
            for label in labels:
                if any(fnmatch.fnmatchcase(label.casefold(), g.casefold()) for g in globs):
                    selected.update(map(int, part.get_topo_faces_of_label_name_pattern(
                        label, prime.NamePatternParams(model))))
            if selected & claimed:
                raise RuntimeError(f"{part.name}: {name} 与其他边界重叠，请修正标签")
            claimed.update(selected)
            groups[name][part.id] = sorted(selected)
        groups.setdefault("wall_other", {})[part.id] = sorted(set(faces)-claimed)
    print("CAD 面标签清单:", json.dumps(inventory, ensure_ascii=False))
    missing = [n for n in patterns if not any(groups[n].values())]
    if missing:
        raise RuntimeError(f"缺少可识别的面标签 {missing}。请在 CAD 中命名或用 --zones-json 映射。")
    return groups


def create_boundaries(model, groups):
    for name, per_part in groups.items():
        if not any(per_part.values()):
            continue
        zone = checked(model.create_zone(name, prime.ZoneType.FACE), f"建立 {name} zone")
        for part in model.parts:
            faces = per_part[part.id]
            if faces:
                checked(part.add_topo_entities_to_zone(zone.zone_id, faces), f"填充 {name}")
                # Canonical private labels keep prism/sizing scopes independent of CAD aliases.
                checked(part.add_labels_on_topo_entities([f"auto_{name}"], faces), f"标签 {name}")
                if name not in ("inlet", "outlet"):
                    checked(part.add_labels_on_topo_entities(["auto_wall"], faces), "壁面标签")


def generate(model, sizing):
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
    for name in ("blades", "inlet", "outlet"):
        c = model.control_data.create_size_control(prime.SizingType.SOFT)
        checked(c.set_scope(prime.ScopeDefinition(model, label_expression=f"auto_{name}")),
                f"{name} 尺寸范围")
        c.set_soft_sizing_params(prime.SoftSizingParams(model,
            max=sizing.blade if name == "blades" else sizing.port, growth_rate=GROWTH))
        controls.append(c.id)
    sf = checked(prime.SizeField(model).compute_volumetric(controls,
        prime.VolumetricSizeFieldComputeParams(model)), "尺寸场")
    for part in model.parts:
        checked(prime.Surfer(model).mesh_topo_faces(part.id, part.get_topo_faces(),
            prime.SurferParams(model, size_field_type=prime.SizeFieldType.VOLUMETRIC,
                               min_size=sizing.minimum, max_size=sizing.base,
                               growth_rate=GROWTH)), f"{part.name} 面网格")
        summary = part.get_summary(prime.PartSummaryParams(model))
        if summary.n_unmeshed_topo_faces or not summary.n_faces:
            raise RuntimeError(f"{part.name}: 存在未划分的面")
        fluid = checked(model.create_zone(f"fluid_{part.id}", prime.ZoneType.VOLUME), "流体 zone")
        checked(part.add_topo_entities_to_zone(fluid.zone_id, part.get_topo_volumes()), "流体体积")
        vc = model.control_data.create_volume_control()
        vc.set_params(prime.VolumeControlParams(model, cell_zonelet_type=prime.CellZoneletType.FLUID))
        checked(vc.set_scope(prime.ScopeDefinition(model,
            entity_type=prime.ScopeEntity.VOLUME, evaluation_type=prime.ScopeEvaluationType.ZONES,
            part_expression=part.name, zone_expression=f"fluid_{part.id}")), "流体范围")
        pc = model.control_data.create_prism_control()
        checked(pc.set_surface_scope(prime.ScopeDefinition(model,
            part_expression=part.name, label_expression="auto_wall")), "边界层壁面")
        checked(pc.set_volume_scope(prime.ScopeDefinition(model,
            entity_type=prime.ScopeEntity.VOLUME, evaluation_type=prime.ScopeEvaluationType.ZONES,
            part_expression=part.name, zone_expression=f"fluid_{part.id}")), "边界层体积")
        pc.set_growth_params(prime.PrismControlGrowthParams(model,
            offset_type=prime.PrismControlOffsetType.UNIFORM, n_layers=LAYERS,
            growth_rate=GROWTH, first_height=sizing.first_height))
        checked(prime.AutoMesh(model).mesh(part.id, prime.AutoMeshParams(model,
            volume_fill_type=prime.VolumeFillType.POLY, max_size=sizing.base,
            size_field_type=prime.SizeFieldType.VOLUMETRIC,
            volume_control_ids=[vc.id], prism_control_ids=[pc.id])), f"{part.name} POLY 体网格")
    # Keep field active through volume meshing, then release controls.
    model.delete_volumetric_size_fields([sf.size_field_id])
    summaries = [p.get_summary(prime.PartSummaryParams(model)) for p in model.parts]
    if any(s.n_cells <= 0 or s.n_poly_cells <= 0 or s.n_prism_cells <= 0 for s in summaries):
        raise RuntimeError("部分流体域缺少体单元、POLY 单元或边界层棱柱单元")
    return sum(s.n_cells for s in summaries)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cad", type=Path, default=Path("FFF-2.scdoc"))
    p.add_argument("--out", type=Path, default=Path("FFF-2.msh"))
    p.add_argument("--zones-json", type=Path, help='JSON: {"inlet":["*入口*"], ...}')
    return p.parse_args()


def main():
    args = parse_args()
    if not prime.__version__.startswith("0.5.") or not (3, 8) <= sys.version_info[:2] <= (3, 11):
        raise RuntimeError("必须使用 Python 3.8–3.11 与 PyPrimeMesh 0.5.x / Prime 24.1")
    if not args.cad.is_file():
        raise FileNotFoundError(f"未找到模型: {args.cad.resolve()}")
    if args.out.suffix.lower() != ".msh":
        raise ValueError("输出必须使用 .msh 扩展名")
    patterns = DEFAULT_PATTERNS
    if args.zones_json:
        patterns = json.loads(args.zones_json.read_text(encoding="utf-8"))
        if set(patterns) != set(DEFAULT_PATTERNS) or any(
            not isinstance(v, list) or not v or any(not isinstance(g, str) or not g for g in v)
            for v in patterns.values()
        ):
            raise ValueError("映射必须包含 inlet/outlet/blades/volute，值为非空 glob 字符串列表")
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
            faces = [int(f) for p in model.parts for f in p.get_topo_faces()]
            low, high, extents = bounds(model, faces)
            print(f"包围盒 min={low}, max={high}; 整体尺寸={extents}（CAD 导入单位）")
            groups = identify_boundaries(model, patterns)
            features = []
            for name in ("inlet", "outlet", "blades"):
                ids = [f for fs in groups[name].values() for f in fs]
                _, _, dims = bounds(model, ids)
                features.extend(d for d in dims if d > max(extents)*1e-8)
            sizing = estimate_sizing(extents, features, scale)
            print(f"第 {attempt}/{MAX_ATTEMPTS} 次: {sizing}; 层数={LAYERS}")
            create_boundaries(model, groups)
            last_count = generate(model, sizing)
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
