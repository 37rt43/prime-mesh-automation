"""Local-only pump meshing, Python 3.11 / PyPrimeMesh 0.5.1 / Prime 24.1.

No server is started on import or --help. --dry-run DOES start local Prime and
import CAD, but never meshes. All lengths are in imported CAD units.
See README.md for the precise scope and API limitations of this release.
"""
from __future__ import annotations

import argparse
import csv
import importlib.metadata
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from main import launch_prime

REGIONS = {
    'Inlet': 'inlet_body', 'Impeller': 'impeller_body',
    'Volute-Reargap-Outlet': 'volute-body', 'Seal_Chamber': 'seal_chamber_body',
}
BOUNDARIES = (
    'inlet', 'outlet', 'interface_1_s', 'interface_1_r', 'interface_2_r',
    'interface_2_s', 'impeller_blade_wall', 'impeller_front_cover',
    'impeller_rear_cover', 'sealchamber-end', 'sealchamber_inlet',
    'seal_chamber_wall', 'inlet_wall', 'volute_wall',
)
WALLS = (
    'impeller_blade_wall', 'impeller_front_cover', 'impeller_rear_cover',
    'sealchamber-end', 'seal_chamber_wall', 'inlet_wall', 'volute_wall',
)
PORTS = tuple(n for n in BOUNDARIES if n not in WALLS)
DEFAULTS = {
    'global_diagonal_ratio': 1 / 60,
    'minimum_global_ratio': 1 / 30,
    'local_span_ratio': 1 / 24,
    'wall_global_ratio': 0.45,
    'port_global_ratio': 0.65,
    'curvature_normal_angle_degrees': 12.0,
    'elements_per_gap': 6,
    'growth_rate': 1.15,
    'refinement_factor': 0.75,
    'prism_first_to_local_ratio': 0.025,
    'prism_total_to_local_ratio': 0.5,
    'prism_max_layers': 12,
    'prism_growth_rate': 1.15,
    'max_skewness': 0.9,
    'max_aspect_ratio': 100.0,
    'min_element_quality': 0.01,
    'preferred_fill': 'POLY',
    'fluent_executable': None,
    'fluent_timeout_seconds': 1800,
}
LIMITATIONS = [
    'Prime 0.5.1 VolumeSearch returns part-level extrema/counts, not bad-cell '
    'IDs/centroids or boundary-adjacent cell subsets; exact bad-cell coordinates '
    'and tongue/edge/interface-neighborhood volume statistics are unavailable.',
    'Face/edge bounding boxes are measured feature-span proxies, NOT measured '
    'seal clearances or curvature radii. CAD curvature/proximity sizing is '
    'evaluated by Prime; those sampled values are not exposed by this script.',
    'Whole named walls/regions are refined conservatively; blade leading/trailing '
    'edges and volute tongue are not independently semantically identified.',
    'Prism values are requested values, not measured final layer thicknesses. '
    'Local layer truncation is delegated to Prime proximity collision handling.',
    'No flow inputs are assumed; first height is geometry-based, not a y+ prediction.',
]
LOG = logging.getLogger('pump_mesh')
prime: Any = None


def checked(result, operation):
    """Never discard a returned server error or warning."""
    LOG.info('%s: %s', operation, result)
    if hasattr(result, 'error_code') and result.error_code != prime.ErrorCode.NOERROR:
        raise RuntimeError(f'{operation}: {result}')
    return result


def encode(value):
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, 'tolist'):
        return value.tolist()
    if hasattr(value, '_jsonify'):
        return value._jsonify()
    raise TypeError(type(value).__name__)


def save_report(report, path, output):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(report, default=encode, ensure_ascii=False,
                               indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)
    rows = [row for attempt in report['attempts'] for row in attempt.get('quality', [])]
    fields = ['iteration', 'stage', 'part', 'metric', 'minimum', 'maximum',
              'failed_count', 'limit', 'minimum_orthogonal_quality']
    with (output / 'mesh_quality_summary.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def face_ids(model, part, name):
    params = prime.NamePatternParams(model)
    faces = set()
    if name in part.get_labels():
        faces.update(map(int, part.get_topo_faces_of_label_name_pattern(name, params)))
    if any(model.get_zone_name(z) == name for z in part.get_face_zones()):
        faces.update(map(int, part.get_topo_faces_of_zone_name_pattern(name, params)))
    return faces


def inventory(model):
    result = []
    for part in model.parts:
        result.append({'part_id': part.id, 'part': part.name,
                       'labels': list(part.get_labels()),
                       'face_zones': [model.get_zone_name(z) for z in part.get_face_zones()],
                       'volume_zones': [model.get_zone_name(z) for z in part.get_volume_zones()]})
    return result


def inspect_names(model, output):
    actual = inventory(model)
    LOG.info('Actual original names: %s', json.dumps(actual, ensure_ascii=False))
    names = {n for p in actual for n in [p['part'], *p['labels'],
                                       *p['face_zones'], *p['volume_zones']]}
    rows = []
    missing = []
    for region, body in REGIONS.items():
        # The region/body pair is accepted as documented aliases.
        found = region in names or body in names
        rows.append({'name': region+' / '+body, 'kind': 'region/body alias', 'exists': found})
        if not found:
            missing.append(region+' / '+body)
    for name in REGIONS.values():
        # Explicit body names in the required-name list must still exist exactly.
        if name not in names:
            missing.append(name)
    for name in BOUNDARIES:
        faces = sum(len(face_ids(model, p, name)) for p in model.parts)
        found = name in names and faces > 0
        rows.append({'name': name, 'kind': 'boundary', 'exists': found,
                     'topo_face_count': faces})
        if not found:
            missing.append(name + (' (empty face scope)' if name in names else ''))
    for p in actual:
        for kind in ('labels', 'face_zones', 'volume_zones'):
            rows.extend({'name': n, 'kind': 'actual_' + kind, 'exists': True,
                         'part': p['part']} for n in p[kind])
    with (output / 'zone_and_boundary_check.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=['name', 'kind', 'exists', 'part',
                                             'topo_face_count'])
        writer.writeheader()
        writer.writerows(rows)
    if missing:
        raise ValueError(f'Missing required exact names/scopes: {missing}; actual names: {actual}')
    # Resolve body membership through exact part/volume names or full-face labels.
    # Never mesh an additional unknown solid as if it were fluid.
    members = {}
    for p in model.parts:
        all_faces = set(map(int, p.get_topo_faces()))
        if not all_faces:
            raise ValueError(f'{p.name}: CAD topology required; no topo faces')
        matches = []
        pvols = {model.get_zone_name(z) for z in p.get_volume_zones()}
        for region, body in REGIONS.items():
            if p.name in (region, body) or pvols.intersection((region, body)) or any(
                face_ids(model, p, n) == all_faces for n in (region, body)
            ):
                matches.append(region)
        if len(matches) != 1:
            raise ValueError(f'{p.name}: ambiguous/unknown fluid region {matches}; '
                             'one region per imported part is required')
        members[p.id] = matches[0]
    if set(members.values()) != set(REGIONS):
        raise ValueError(f'Four fluid regions not resolved: {members}')
    for p in model.parts:
        scopes = {n: face_ids(model, p, n) for n in BOUNDARIES}
        owner = {}
        for n, fs in scopes.items():
            for face in fs:
                if face in owner:
                    raise ValueError(f'{p.name}: overlapping boundary scopes: {owner[face]}, {n}, face={face}')
                owner[face] = n
        uncovered = set(map(int, p.get_topo_faces())) - owner.keys()
        if uncovered:
            raise ValueError(f'{p.name}: faces without explicit wall/port/interface classification: {sorted(uncovered)}')
    return actual, members


def bbox(model, zonelets):
    if not zonelets:
        raise ValueError('Empty geometry zonelets')
    b = prime.SurfaceUtilities(model).get_bounding_box_of_zonelets(list(zonelets))
    low, high = [b.xmin, b.ymin, b.zmin], [b.xmax, b.ymax, b.zmax]
    if not all(math.isfinite(v) for v in low + high):
        raise ValueError('Nonfinite geometry bounds')
    return {'low': low, 'high': high, 'extents': [v-u for u, v in zip(low, high)]}


def measure(model):
    all_z = []
    measured = {}
    for p in model.parts:
        fs = list(p.get_topo_faces())
        zs = list(model.topo_data.get_geom_zonelets_of_topo_faces(fs))
        all_z.extend(zs)
        part = bbox(model, zs)
        spans = []
        # Use each edge's diagonal, never its smallest Cartesian extent: a
        # tilted planar face's bbox thickness is not a physical clearance.
        for e in p.get_topo_edges():
            ez = list(model.topo_data.get_geom_zonelets_of_topo_edges([int(e)]))
            if ez:
                ext = bbox(model, ez)['extents']
                length = math.sqrt(sum(v*v for v in ext))
                if length > 0:
                    spans.append(length)
        part['edge_bbox_diagonals'] = spans
        part['boundaries'] = {}
        for n in BOUNDARIES:
            faces = face_ids(model, p, n)
            if faces:
                z = list(model.topo_data.get_geom_zonelets_of_topo_faces(sorted(faces)))
                part['boundaries'][n] = bbox(model, z)
        measured[p.id] = part
    return {'overall': bbox(model, all_z), 'parts': measured,
            'seal_gap': None, 'curvature_radii': None,
            'measurement_notes': LIMITATIONS[1]}


def sizing(geometry, cfg, iteration, failed_parts):
    d = math.sqrt(sum(v*v for v in geometry['overall']['extents']))
    if d <= 0:
        raise ValueError('Degenerate overall geometry')
    base = d * cfg['global_diagonal_ratio']
    result = {'diagonal': d, 'base': base, 'parts': {}, 'ratios': cfg.copy()}
    for pid, p in geometry['parts'].items():
        factor = cfg['refinement_factor'] ** iteration if pid in failed_parts else 1.0
        pd = math.sqrt(sum(v*v for v in p['extents']))
        significant = sorted(v for v in p['edge_bbox_diagonals'] if v > d*1e-10)
        feature = significant[max(0, len(significant)//10)] if significant else pd
        local = min(base*cfg['wall_global_ratio'], feature*cfg['local_span_ratio']) * factor
        minimum = min(base*cfg['minimum_global_ratio'], local/4)
        if not (0 < minimum <= local <= base):
            raise ValueError(f'Invalid size derived for part {pid}')
        growth = min(cfg['prism_growth_rate'], 1 + 1/max(8.0, feature/local))
        first = local*cfg['prism_first_to_local_ratio']*cfg['refinement_factor']**iteration
        budget = min(local*cfg['prism_total_to_local_ratio'], feature/4)
        layers = max(1, min(cfg['prism_max_layers'], int(
            math.log1p(budget*(growth-1)/first)/math.log(growth))))
        requested = first*(growth**layers-1)/(growth-1)
        result['parts'][pid] = {'local': local, 'minimum': minimum, 'base': base,
            'edge_span_proxy': feature, 'first_height_requested': first,
            'layer_count_requested': layers, 'prism_growth': growth,
            'total_thickness_requested': requested, 'actual_thickness': None,
            'gap_measured': None, 'local_refinement_factor': factor}
    smallest = min(s['minimum'] for s in result['parts'].values())
    steps = max(12, math.ceil(d/base))
    result['growth_rate'] = min(cfg['growth_rate'], (base/smallest)**(1/steps))
    LOG.info('Derived sizing and assumptions: %s', json.dumps(result))
    return result


def scope(model, part=None, label='*', volume=False):
    return prime.ScopeDefinition(model, part_expression=part or '*',
        label_expression=label, zone_expression='*',
        entity_type=prime.ScopeEntity.VOLUME if volume else prime.ScopeEntity.FACEZONELETS,
        evaluation_type=prime.ScopeEvaluationType.ZONES if volume else prime.ScopeEvaluationType.LABELS)


def prepare_boundaries(model):
    # Named selections are materialized as face zones with the SAME exact name.
    # Unknown imported labels remain intact. No merging/stitching/renaming.
    for n in BOUNDARIES:
        existing = {z for p in model.parts for z in p.get_face_zones()
                    if model.get_zone_name(z) == n}
        if len(existing) > 1:
            raise ValueError(f'Duplicate exact face zone name: {n}')
        zone = next(iter(existing), None)
        for p in model.parts:
            fs = sorted(face_ids(model, p, n))
            if not fs:
                continue
            if zone is None:
                zone = checked(model.create_zone(n, prime.ZoneType.FACE), 'create '+n).zone_id
                if model.get_zone_name(zone) != n:
                    raise ValueError(f'Server changed requested name {n}')
            checked(p.add_topo_entities_to_zone(zone, fs), 'preserve '+n)
            checked(p.add_labels_on_topo_entities(['__pump_all'], fs), 'all-surface scope')
            if n in WALLS:
                checked(p.add_labels_on_topo_entities(['__pump_wall'], fs), 'explicit wall scope')
            checked(p.add_labels_on_topo_entities(['__pump_'+n], fs), 'sizing scope '+n)


def mesh(model, sizes, cfg, members, force_tet):
    minimum = min(v['minimum'] for v in sizes['parts'].values())
    model.set_global_sizing_params(prime.GlobalSizingParams(model,
        min=minimum, max=sizes['base'], growth_rate=sizes['growth_rate']))
    controls = []
    for p in model.parts:
        s = sizes['parts'][p.id]
        common = dict(min=s['minimum'], max=s['base'], growth_rate=sizes['growth_rate'])
        for kind in ('CURVATURE', 'PROXIMITY'):
            c = model.control_data.create_size_control(getattr(prime.SizingType, kind))
            checked(c.set_scope(scope(model, p.name, '__pump_all')), kind+' scope')
            if kind == 'CURVATURE':
                c.set_curvature_sizing_params(prime.CurvatureSizingParams(model, **common,
                    normal_angle=cfg['curvature_normal_angle_degrees'], use_cad_curvature=True))
            else:
                c.set_proximity_sizing_params(prime.ProximitySizingParams(model, **common,
                    elements_per_gap=cfg['elements_per_gap'], ignore_self_proximity=False,
                    ignore_orientation=True))
            controls.append(c.id)
        # All named walls including blade edges, tongue and bends are included.
        # Curvature/proximity further reduce size at geometric features.
        for n in BOUNDARIES:
            if not face_ids(model, p, n):
                continue
            c = model.control_data.create_size_control(prime.SizingType.SOFT)
            checked(c.set_scope(scope(model, p.name, '__pump_'+n)), 'local '+n)
            local = s['local'] if n in WALLS or n.startswith('interface') else min(
                s['local'], s['base']*cfg['port_global_ratio'])
            c.set_soft_sizing_params(prime.SoftSizingParams(model, max=local,
                                                          growth_rate=sizes['growth_rate']))
            controls.append(c.id)
    for p in model.parts:
        prime.lucid.Mesh(model).surface_mesh_with_size_controls(
            scope=prime.lucid.SurfaceScope(part_expression=p.name))
        summary = p.get_summary(prime.PartSummaryParams(model))
        if summary.n_unmeshed_topo_faces or summary.n_faces <= 0:
            raise RuntimeError(f'{p.name}: surface meshing incomplete: {summary}')
    sf = checked(prime.SizeField(model).compute_volumetric(controls,
        prime.VolumetricSizeFieldComputeParams(model)), 'volumetric size field')
    fills = {}
    try:
        for p in model.parts:
            s = sizes['parts'][p.id]
            if not len(p.get_topo_volumes()):
                checked(p.compute_topo_volumes(prime.ComputeVolumesParams(model)), 'closed fluid volumes')
            if not len(p.get_topo_volumes()):
                raise ValueError(f'{p.name}: no closed fluid volume')
            if not len(p.get_volume_zones()):
                name = REGIONS[members[p.id]]
                z = checked(model.create_zone(name, prime.ZoneType.VOLUME), 'fluid '+name).zone_id
                if model.get_zone_name(z) != name:
                    raise ValueError('Volume zone exact-name collision: '+name)
                checked(p.add_topo_entities_to_zone(z, p.get_topo_volumes()), 'fluid membership')
            vc = model.control_data.create_volume_control()
            vc.set_params(prime.VolumeControlParams(model, cell_zonelet_type=prime.CellZoneletType.FLUID))
            checked(vc.set_scope(scope(model, p.name, volume=True)), 'fluid volume scope')
            prism_ids = []
            if '__pump_wall' in p.get_labels():
                pc = model.control_data.create_prism_control()
                checked(pc.set_surface_scope(scope(model, p.name, '__pump_wall')), 'wall-only prism')
                checked(pc.set_volume_scope(scope(model, p.name, volume=True)), 'prism volume')
                pc.set_growth_params(prime.PrismControlGrowthParams(model,
                    offset_type=prime.PrismControlOffsetType.UNIFORM,
                    n_layers=s['layer_count_requested'], growth_rate=s['prism_growth'],
                    first_height=s['first_height_requested']))
                prism_ids.append(pc.id)
            preferred = cfg['preferred_fill'] if members[p.id] == 'Volute-Reargap-Outlet' else 'TET'
            fill = 'TET' if force_tet else preferred
            if not hasattr(prime.VolumeFillType, fill):
                LOG.warning('VolumeFillType.%s unavailable; falling back to TET', fill)
                fill = 'TET'
            fills[p.id] = fill
            checked(prime.AutoMesh(model).mesh(p.id, prime.AutoMeshParams(model,
                volume_fill_type=getattr(prime.VolumeFillType, fill), max_size=s['base'],
                size_field_type=prime.SizeFieldType.VOLUMETRIC,
                prism=prime.PrismParams(model, stair_step=prime.PrismStairStep(
                    model, check_proximity=True, gap_factor_scale=0.5)),
                volume_control_ids=[vc.id], prism_control_ids=prism_ids)), 'volume '+p.name+' '+fill)
            summary = p.get_summary(prime.PartSummaryParams(model))
            if summary.n_cells <= 0 or (prism_ids and summary.n_prism_cells <= 0):
                raise RuntimeError(f'{p.name}: missing volume/prism cells: {summary}')
    finally:
        model.delete_volumetric_size_fields([sf.size_field_id])
    return fills


def quality(model, cfg, target, iteration, stage):
    metric_names = ['INVERSEORTHOGONAL', 'SKEWNESS', 'ASPECTRATIO', 'ELEMENTQUALITY']
    limits = [1-target, cfg['max_skewness'], cfg['max_aspect_ratio'], cfg['min_element_quality']]
    results = checked(prime.VolumeSearch(model).get_volume_quality_summary(
        prime.VolumeQualitySummaryParams(model,
            cell_quality_measures=[getattr(prime.CellQualityMeasure, n) for n in metric_names],
            scope=scope(model, volume=True), quality_limit=limits)), 'quality '+stage)
    parts = {p.id: p.name for p in model.parts}
    rows, failed, seen = [], set(), set()
    for r in results.quality_results_part:
        n = r.cell_quality_measure.name
        lo, hi = float(r.min_quality), float(r.max_quality)
        if not all(math.isfinite(v) for v in (lo, hi)):
            raise ValueError('Nonfinite mesh quality')
        seen.add((r.part_id, n))
        q = 1-hi if n == 'INVERSEORTHOGONAL' else None
        row = {'iteration': iteration, 'stage': stage, 'part': parts[r.part_id],
               'part_id': r.part_id, 'metric': n, 'minimum': lo, 'maximum': hi,
               'failed_count': int(r.n_found), 'limit': float(r.quality_limit),
               'minimum_orthogonal_quality': q, 'bad_cell_centroids': None,
               'coordinate_status': 'not exposed by Prime 0.5.1 summary API'}
        rows.append(row)
        bad = (q <= target if q is not None else
               hi > cfg['max_skewness'] if n == 'SKEWNESS' else
               hi > cfg['max_aspect_ratio'] if n == 'ASPECTRATIO' else
               lo < cfg['min_element_quality'])
        if bad:
            failed.add(r.part_id)
            LOG.warning('Failed volume quality: %s', row)
    expected = {(p.id, n) for p in model.parts for n in metric_names}
    if seen != expected:
        raise RuntimeError(f'Incomplete quality results: missing={expected-seen}, extra={seen-expected}')
    return rows, failed


def surface_diagnostics(model):
    diagnostics = []
    for p in model.parts:
        for name in BOUNDARIES:
            if not face_ids(model, p, name):
                continue
            result = checked(prime.SurfaceSearch(model).get_surface_quality_summary(
                prime.SurfaceQualitySummaryParams(model,
                    scope=scope(model, p.name, '__pump_'+name),
                    face_quality_measures=[prime.FaceQualityMeasure.SKEWNESS,
                                           prime.FaceQualityMeasure.ASPECTRATIO,
                                           prime.FaceQualityMeasure.ELEMENTQUALITY])),
                'named surface quality '+p.name+'/'+name)
            diagnostics.append({'part': p.name, 'boundary': name,
                                'statistic_type': 'surface_faces_only_not_adjacent_cells',
                                'result': result._jsonify()})
    return diagnostics


def smooth(model, failed, target):
    for p in model.parts:
        if p.id in failed:
            # Empty movable boundary scope requests interior-node repair only.
            checked(prime.VolumeMeshTool(model).improve_by_auto_node_move(
                p.id, p.get_cell_zonelets(), [], prime.AutoNodeMoveParams(model,
                    quality_measure=prime.CellQualityMeasure.INVERSEORTHOGONAL,
                    target_quality=1-target, n_attempts=3, n_iterations_per_node=5,
                    restrict_boundary_nodes_along_surface=True)),
                'interior-node quality repair '+p.name)


def verify_names_preserved(model, before):
    after = inventory(model)
    by_id = {p['part_id']: p for p in after}
    for p in before:
        q = by_id.get(p['part_id'])
        if q is None or q['part'] != p['part']:
            raise RuntimeError('Original part identity changed')
        for kind in ('labels', 'face_zones', 'volume_zones'):
            missing = set(p[kind]) - set(q[kind])
            if missing:
                raise RuntimeError(f'Original {kind} lost in {p["part"]}: {missing}')
    for n in BOUNDARIES:
        if not any(n in p['face_zones'] for p in after):
            raise RuntimeError('Export boundary zone missing: '+n)
    return after


def export_hdf5(model, output, cfg):
    """0.5.1 documents MSH and CAS.H5, not MSH.H5; convert via local Fluent."""
    import h5py
    raw = output / 'pump_intermediate.msh'
    checked(prime.FileIO(model).export_fluent_meshing_mesh(str(raw),
        prime.ExportFluentMeshingMeshParams(model)), 'export native Fluent MSH')
    if not raw.is_file() or raw.stat().st_size == 0:
        raise RuntimeError('Prime export is empty')
    fluent = cfg['fluent_executable'] or shutil.which('fluent')
    if not fluent:
        root = os.environ.get('AWP_ROOT241')
        if root:
            candidate = Path(root)/'fluent'/'ntbin'/'win64'/'fluent.exe'
            if candidate.is_file():
                fluent = str(candidate)
    if not fluent:
        raise RuntimeError('MSH.H5 requires local Fluent 2024R1 conversion; set '
                           'fluent_executable or AWP_ROOT241. Native .msh retained.')
    final = output / 'pump.msh.h5'
    candidate = output / 'pump_candidate.msh.h5'
    candidate.unlink(missing_ok=True)
    def quote(path):
        s = str(path).replace('\\', '/')
        if any(ch in s for ch in ('"', '\n', '\r')):
            raise ValueError('Journal path contains unsupported quote/newline')
        return '"'+s+'"'
    journal = output / 'convert_to_hdf5.jou'
    journal.write_text('/file/read-mesh '+quote(raw)+'\n'
                       '/file/cff-files? yes\n'
                       '/file/write-mesh '+quote(candidate)+'\n'
                       '/exit yes\n', encoding='utf-8')
    with (output/'fluent_conversion.log').open('w', encoding='utf-8') as stream:
        subprocess.run([str(fluent), '3ddp', '-g', '-t1',
                        *(['-wait'] if os.name == 'nt' else []), '-i', str(journal)],
                       cwd=output, stdout=stream, stderr=subprocess.STDOUT,
                       timeout=cfg['fluent_timeout_seconds'], check=True)
    if not candidate.exists() or not h5py.is_hdf5(candidate):
        raise RuntimeError('Fluent did not produce real HDF5 mesh; inspect conversion log')
    with h5py.File(candidate, 'r') as h5:
        if 'meshes' not in h5:
            raise RuntimeError('HDF5 output lacks Fluent meshes group')
    candidate.replace(final)
    return final


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cad', type=Path, default=Path('./FFF-8.scdoc'))
    p.add_argument('--output', type=Path, default=Path('./output'))
    p.add_argument('--target-orthogonal-quality', type=float, default=0.1)
    p.add_argument('--max-iterations', type=int, default=4, help='maximum remesh repairs after initial attempt')
    p.add_argument('--mesh-format', choices=['Fluent', 'fluent'], default='Fluent')
    p.add_argument('--dry-run', action='store_true', help='LOCAL Prime import/name/geometry checks only')
    p.add_argument('--report', type=Path)
    p.add_argument('--config', type=Path)
    args = p.parse_args(argv)
    if not 0.1 <= args.target_orthogonal_quality < 1:
        p.error('--target-orthogonal-quality must be >= 0.1 and < 1')
    if args.max_iterations < 0:
        p.error('--max-iterations must be >= 0')
    return args


def configuration(path):
    cfg = DEFAULTS.copy()
    if path:
        loaded = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(loaded, dict) or set(loaded)-set(cfg):
            raise ValueError('Unknown configuration keys or invalid JSON object')
        cfg.update(loaded)
    for key, val in cfg.items():
        if key not in ('preferred_fill', 'fluent_executable'):
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
                raise ValueError('Configuration must be finite and positive: '+key)
    for key in ('global_diagonal_ratio', 'minimum_global_ratio', 'local_span_ratio',
                'wall_global_ratio', 'port_global_ratio', 'refinement_factor',
                'prism_first_to_local_ratio', 'prism_total_to_local_ratio',
                'max_skewness', 'min_element_quality'):
        if cfg[key] >= 1:
            raise ValueError('Ratio must be < 1: '+key)
    if not (1 < cfg['growth_rate'] <= 1.3 and 1 < cfg['prism_growth_rate'] <= 1.3):
        raise ValueError('Growth rates must lie in (1, 1.3]')
    for key in ('prism_max_layers', 'elements_per_gap', 'fluent_timeout_seconds'):
        if not isinstance(cfg[key], int):
            raise ValueError('Expected integer: '+key)
    if cfg['preferred_fill'] not in ('POLY', 'HEXCOREPOLY', 'TET'):
        raise ValueError('preferred_fill must be POLY, HEXCOREPOLY or TET')
    return cfg


def run(argv=None):
    global prime
    args = arguments(argv)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Do not allow stale output from an earlier successful run to masquerade as this run.
    if (output/'pump.msh.h5').exists():
        raise FileExistsError('Use a fresh output directory; pump.msh.h5 already exists')
    handlers = [logging.StreamHandler(), logging.FileHandler(output/'meshing_log.txt', encoding='utf-8')]
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format='%(asctime)s %(levelname)s %(message)s', force=True)
    report_path = args.report.resolve() if args.report else output/'mesh_quality_report.json'
    report = {'status': 'started', 'cad': str(args.cad.resolve()), 'attempts': [],
              'limitations': LIMITATIONS, 'validation': 'local execution required',
              'precise_feature_volume_statistics': None, 'actual_prism_thickness': None}
    save_report(report, report_path, output)
    try:
        if not args.cad.is_file():
            raise FileNotFoundError(f'CAD file not found: {args.cad.resolve()}')
        if sys.version_info[:2] != (3, 11):
            raise RuntimeError('Use Python 3.11 for this 2024R1 workflow')
        if importlib.metadata.version('ansys-meshing-prime') != '0.5.1':
            raise RuntimeError('Exactly ansys-meshing-prime==0.5.1 required')
        import ansys.meshing.prime as prime_module
        prime = prime_module
        cfg = configuration(args.config)
        report['configuration'] = cfg
        LOG.info('Python=%s, PyPrimeMesh=%s; CAD=%s', sys.version, prime.__version__, args.cad.resolve())
        for note in LIMITATIONS:
            LOG.warning(note)
        failed_parts, force_tet = set(), not hasattr(prime.VolumeFillType, cfg['preferred_fill'])
        for iteration in range(args.max_iterations+1):
            attempt = {'iteration': iteration, 'quality': []}
            report['attempts'].append(attempt)
            with launch_prime() as client:
                model = client.model
                checked(prime.FileIO(model).import_cad(str(args.cad.resolve()),
                    prime.ImportCadParams(model, geometry_transfer=True)), 'import CAD')
                original, members = inspect_names(model, output)
                geometry = measure(model)
                report['geometry'] = geometry
                report['original_names'] = original
                if args.dry_run:
                    report['status'] = 'dry_run_geometry_and_names_passed'
                    return 0
                # IDs may differ on reimport: use exact part names to carry failure scope.
                failed_ids = {p.id for p in model.parts if p.name in failed_parts}
                sizes = sizing(geometry, cfg, iteration, failed_ids)
                attempt['requested_sizing'] = sizes
                prepare_boundaries(model)
                try:
                    attempt['fill_types'] = mesh(model, sizes, cfg, members, force_tet)
                except RuntimeError as exc:
                    attempt['meshing_error'] = str(exc)
                    if not force_tet and iteration < args.max_iterations:
                        LOG.exception('Mesh attempt failed; reimport original CAD and retry TET')
                        force_tet = True
                        failed_parts = {p.name for p in model.parts}
                        save_report(report, report_path, output)
                        continue
                    raise
                rows, failed = quality(model, cfg, args.target_orthogonal_quality, iteration, 'initial')
                attempt['quality'].extend(rows)
                if failed:
                    smooth(model, failed, args.target_orthogonal_quality)
                    rows, failed = quality(model, cfg, args.target_orthogonal_quality, iteration, 'after_smoothing')
                    attempt['quality'].extend(rows)
                attempt['summaries'] = {p.name: p.get_summary(prime.PartSummaryParams(model))._jsonify()
                                        for p in model.parts}
                attempt['named_surface_quality'] = surface_diagnostics(model)
                report['final_names'] = verify_names_preserved(model, original)
                if not failed:
                    report['mesh'] = str(export_hdf5(model, output, cfg))
                    report['status'] = 'part_quality_passed_with_documented_diagnostic_limitations'
                    report['validation'] = 'Prime part-level checks passed; Fluent inspection still required'
                    LOG.info('Export completed; this is not certification of unsupported local diagnostics')
                    return 0
                failed_parts = {p.name for p in model.parts if p.id in failed}
                LOG.warning('Reimport/remesh failed parts with reduced sizing and prism height: %s', failed_parts)
            save_report(report, report_path, output)
        raise RuntimeError('Quality repair limit exhausted; no final .msh.h5 exported')
    except Exception as exc:
        report['status'] = 'failed'
        report['error'] = str(exc)
        LOG.exception('Meshing workflow failed')
        return 2
    finally:
        save_report(report, report_path, output)


if __name__ == '__main__':
    raise SystemExit(run())
