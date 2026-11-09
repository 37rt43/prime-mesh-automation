"""Local-only pump meshing, Python 3.11 / PyPrimeMesh 0.5.1 / Prime 24.1.

No server is started on import or --help. --dry-run DOES start local Prime and
import CAD and recognize boundaries, but never meshes. All lengths are in imported CAD units.
See README.md for the precise scope and API limitations of this release.
"""
from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager
import faulthandler
from functools import wraps
import threading
import time
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

BOUNDARIES = ()
WALLS = ()
AUTO_FACE_SCOPES = {}
AUTO_MAPPING = []
AUTO_RECOGNITION = {}
DEFAULTS = {
    'recognition_budget_seconds': 120,
    'recognition_contact_ratio': 1e-5,
    'recognition_contact_coverage': 0.98,
    'recognition_planarity_ratio': 1e-4,
    'recognition_samples': 128,
    'coarse_diagonal_ratio': 1 / 30,
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
    'Whole automatically classified walls/regions are refined conservatively; blade leading/trailing '
    'edges and volute tongue are not independently semantically identified.',
    'Prism values are requested values, not measured final layer thicknesses. '
    'Local layer truncation is delegated to Prime proximity collision handling.',
    'No flow inputs are assumed; first height is geometry-based, not a y+ prediction.',
    'Boundary types and region roles are geometric/name-hint hypotheses. Unknown faces '
    'default to uncertain wall. Flow direction, material identity and partial-contact '
    'face splitting require user review in Fluent.',
]
LOG = logging.getLogger('pump_mesh')
prime: Any = None


MONITOR = None
QUERY_CACHE = {}
QUERY_OWNERS = {}


def cached_query(owner, method, *args):
    """Cache read-only CAD metadata within one import/model generation."""
    def freeze(value):
        if isinstance(value, (list, tuple)) or hasattr(value, 'tolist'):
            return tuple(freeze(v) for v in value)
        return value
    QUERY_OWNERS[id(owner)] = owner  # prevent object-ID reuse while metadata is cached
    key = (id(owner), method, tuple(freeze(a) for a in args))
    if key not in QUERY_CACHE:
        QUERY_CACHE[key] = getattr(owner, method)(*args)
    return QUERY_CACHE[key]


def invalidate_queries():
    QUERY_CACHE.clear()
    QUERY_OWNERS.clear()


class RuntimeMonitor:
    """Diagnostics only: never run concurrent Prime RPCs or kill a busy server."""
    def __init__(self, output, timeout, interval=30):
        self.output, self.timeout, self.interval = output, timeout, interval
        self.lock, self.stop_event = threading.RLock(), threading.Event()
        self.stages, self.calls, self.warned = [], [], set()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.watch, name='meshing-heartbeat', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2)

    @contextmanager
    def activity(self, name, rpc=False):
        entry = (name, time.monotonic())
        stack = self.calls if rpc else self.stages
        with self.lock:
            stack.append(entry)
        LOG.log(logging.DEBUG if rpc else logging.INFO, '%s BEGIN %s', 'PRIME' if rpc else 'STAGE', name)
        status = 'OK'
        try:
            yield
        except BaseException:
            status = 'FAILED'
            raise
        finally:
            LOG.log(logging.DEBUG if rpc else logging.INFO, '%s END %s status=%s elapsed=%.3fs',
                    'PRIME' if rpc else 'STAGE', name, status, time.monotonic()-entry[1])
            with self.lock:
                stack.remove(entry)

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            stage = self.stages[-1] if self.stages else ('idle', now)
            call = self.calls[-1] if self.calls else ('none', now)
            return dict(stage=stage[0], stage_elapsed_seconds=now-stage[1],
                        prime_call=call[0], prime_call_elapsed_seconds=now-call[1],
                        timeout_diagnostic_seconds=self.timeout, timeout_is_diagnostic_only=True)

    def pulse(self):
        state = self.snapshot()
        LOG.info('HEARTBEAT stage=%s elapsed=%.1fs Prime=%s call_elapsed=%.1fs',
                 state['stage'], state['stage_elapsed_seconds'], state['prime_call'],
                 state['prime_call_elapsed_seconds'])
        with self.lock:
            active = self.calls[-1] if self.calls else (self.stages[-1] if self.stages else None)
        if active and time.monotonic()-active[1] >= self.timeout and active not in self.warned:
            self.warned.add(active)
            LOG.error('TIMEOUT DIAGNOSTIC: %s exceeded %.1fs; server may still be working. '
                      'No concurrent RPC/retry/cancellation is issued. Inspect Prime CPU/logs and stacks.',
                      active[0], self.timeout)
            with (self.output/'prime_timeout_stacks.txt').open('a') as stream:
                faulthandler.dump_traceback(file=stream, all_threads=True)
        temp = self.output/'prime_runtime_status.json.tmp'
        temp.write_text(json.dumps(state, indent=2), encoding='utf-8')
        temp.replace(self.output/'prime_runtime_status.json')

    def watch(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.pulse()
            except Exception:
                LOG.exception('Heartbeat diagnostics failed; meshing thread remains untouched')


@contextmanager
def phase(name):
    if MONITOR is None:
        yield
    else:
        with MONITOR.activity(name):
            yield


def timed_stage(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with phase(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


@contextmanager
def observe_prime_calls(model):
    """0.5.1 communicator hooks add diagnostics, not unsupported RPC timeout kwargs."""
    comm = model._communicator
    originals = {}
    try:
        for method in ('serve', 'initialize_params', 'import_cad'):
            if not hasattr(comm, method):
                continue
            original = getattr(comm, method)
            originals[method] = original
            def wrapped(*args, _original=original, _method=method, **kwargs):
                command = str(args[1]) if len(args) > 1 else _method
                if MONITOR is None:
                    return _original(*args, **kwargs)
                with MONITOR.activity(_method+': '+command, rpc=True):
                    return _original(*args, **kwargs)
            setattr(comm, method, wrapped)
        yield
    finally:
        for method, original in originals.items():
            setattr(comm, method, original)


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
    if name in BOUNDARIES:
        return set(AUTO_FACE_SCOPES.get((part.id, name), set()))
    if (part.id, name) in AUTO_FACE_SCOPES:
        return set(AUTO_FACE_SCOPES[(part.id, name)])
    lookup_key = ('original_faces', id(part), name)
    if lookup_key in QUERY_CACHE:
        return set(QUERY_CACHE[lookup_key])
    params = prime.NamePatternParams(model)
    faces = set()
    if name in cached_query(part, 'get_labels'):
        faces.update(map(int, cached_query(part, 'get_topo_faces_of_label_name_pattern', name, params)))
    if any(cached_query(model, 'get_zone_name', z) == name for z in cached_query(part, 'get_face_zones')):
        faces.update(map(int, cached_query(part, 'get_topo_faces_of_zone_name_pattern', name, params)))
    QUERY_CACHE[lookup_key] = frozenset(faces)
    return faces


def inventory(model):
    result = []
    for part in model.parts:
        result.append({'part_id': part.id, 'part': part.name,
                       'labels': list(cached_query(part, 'get_labels')),
                       'face_zones': [cached_query(model, 'get_zone_name', z) for z in cached_query(part, 'get_face_zones')],
                       'volume_zones': [cached_query(model, 'get_zone_name', z) for z in cached_query(part, 'get_volume_zones')]})
    return result


# Automatic boundary recognition uses CAD display facets, not a generated mesh.
class RecognitionBudgetExceeded(RuntimeError):
    pass


def recognition_checkpoint(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise RecognitionBudgetExceeded('automatic classification CPU budget reached')


def facet_descriptor(points, polygons):
    """Measure a CAD face using VTK-style count-prefixed facet connectivity."""
    import numpy as np
    points = np.asarray(points, dtype=float).reshape((-1, 3))
    cells = np.asarray(polygons, dtype=int).ravel()
    triangles, edges, pos = [], {}, 0
    while pos < len(cells):
        n = int(cells[pos])
        ids = cells[pos+1:pos+1+n]
        if n < 3 or len(ids) != n or np.any(ids < 0) or np.any(ids >= len(points)):
            raise ValueError('Malformed CAD display facet connectivity')
        for a, b in zip(ids, np.roll(ids, -1)):
            edge = tuple(sorted((int(a), int(b))))
            edges[edge] = edges.get(edge, 0)+1
        triangles.extend([points[[ids[0], ids[i], ids[i+1]]] for i in range(1, n-1)])
        pos += n+1
    tri = np.asarray(triangles, dtype=float).reshape((-1, 3, 3))
    if not len(tri) or not np.all(np.isfinite(points)):
        raise ValueError('Missing/nonfinite CAD display facets')
    vectors = np.cross(tri[:, 1]-tri[:, 0], tri[:, 2]-tri[:, 0])/2
    weights = np.linalg.norm(vectors, axis=1)
    area = float(weights.sum())
    if area <= 0:
        raise ValueError('Degenerate CAD face facets')
    center = (tri.mean(axis=1)*weights[:, None]).sum(axis=0)/area
    _, singular, axes = np.linalg.svd(points-center, full_matrices=False)
    normal = axes[-1]
    span = max(float(np.linalg.norm(np.ptp(points, axis=0))), 1e-300)
    planarity = float(np.max(np.abs((points-center)@normal))/span)
    coherence = float(np.linalg.norm(vectors.sum(axis=0))/area)
    boundary = sorted({i for edge, count in edges.items() if count == 1 for i in edge})
    ring = points[boundary] if boundary else points
    radii = np.linalg.norm((ring-center)-np.outer((ring-center)@normal, normal), axis=1)
    outer = radii[radii >= 0.8*max(float(radii.max()), 1e-300)]
    circularity = float(np.std(outer)/max(float(np.mean(outer)), 1e-300)) if len(outer) >= 6 else 1.0
    aspect = float(singular[1]/max(singular[0], 1e-300)) if len(singular) >= 2 else 0.0
    return dict(points=points, triangles=tri, area=area, center=center, normal=normal,
                low=points.min(axis=0), high=points.max(axis=0),
                planarity=planarity, normal_coherence=coherence,
                circular_radius_variation=circularity, planar_aspect=aspect,
                radius=float(radii.max()), measurement_status='CAD_display_facets_approximate')


def surface_distance(points, triangles, deadline=None):
    """Exact point-to-triangle distances for the supplied tessellated surfaces."""
    import numpy as np
    t = np.asarray(triangles, dtype=float).reshape((-1, 3, 3))
    if not len(t):
        return np.full(len(points), np.inf)
    distances = []
    for point in points:
        # Chunk triangle work to bound memory even on heavily faceted CAD.
        best = float('inf')
        for start in range(0, len(t), 4096):
            recognition_checkpoint(deadline)
            chunk = t[start:start+4096]
            a, b, c = chunk[:, 0], chunk[:, 1], chunk[:, 2]
            ab, ac, ap = b-a, c-a, point-a
            cross = np.cross(ab, ac)
            n2 = np.sum(cross*cross, axis=1)
            valid = n2 > 0
            signed = np.sum(ap*cross, axis=1)
            projection = ap-cross*np.divide(signed, n2, out=np.zeros_like(signed), where=valid)[:, None]
            u0 = np.sum(ab*ab, axis=1)
            u1 = np.sum(ab*ac, axis=1)
            u2 = np.sum(ac*ac, axis=1)
            u3 = np.sum(projection*ab, axis=1)
            u4 = np.sum(projection*ac, axis=1)
            det = u0*u2-u1*u1
            u = np.divide(u2*u3-u1*u4, det, out=np.full_like(det, -1), where=det > 0)
            v = np.divide(u0*u4-u1*u3, det, out=np.full_like(det, -1), where=det > 0)
            inside = valid & (u >= -1e-10) & (v >= -1e-10) & (u+v <= 1+1e-10)
            plane = np.divide(signed*signed, n2, out=np.full_like(n2, np.inf), where=valid)
            sq = np.where(inside, plane, np.inf)
            for x, y in ((a, b), (b, c), (c, a)):
                delta = y-x
                length2 = np.sum(delta*delta, axis=1)
                alpha = np.clip(np.divide(np.sum((point-x)*delta, axis=1), length2,
                    out=np.zeros_like(length2), where=length2 > 0), 0, 1)
                sq = np.minimum(sq, np.sum((point-x-alpha[:, None]*delta)**2, axis=1))
            best = min(best, float(sq.min()))
        distances.append(math.sqrt(best))
    return np.asarray(distances)


def contact_coverage(face, others, tolerance, samples, deadline=None):
    """Area-weighted deterministic facet samples; not a bbox-only contact test."""
    import numpy as np
    if not others or not len(face['triangles']):
        return 0.0
    tri = face['triangles']
    weights = np.linalg.norm(np.cross(tri[:, 1]-tri[:, 0], tri[:, 2]-tri[:, 0]), axis=1)
    # Stratified sampling of cumulative triangle area avoids small-facet bias.
    q = (np.arange(min(samples, len(tri)))+0.5)/min(samples, len(tri))
    ids = np.searchsorted(np.cumsum(weights)/weights.sum(), q)
    targets = np.concatenate([f['triangles'] for f in others])
    centroids = tri[ids].mean(axis=1)
    # Vertices catch sliver overhangs missed by centroid tests.
    probes = np.concatenate((centroids, tri[ids].reshape((-1, 3))))
    distances = surface_distance(probes, targets, deadline=deadline)
    return float(np.mean(distances <= tolerance))


def infer_regions(parts, faces, contacts):
    """Use optional name evidence, then geometry/contact graph; never require names."""
    import numpy as np
    neighbors = {pid: set() for pid in parts}
    for a, b in contacts:
        if a[0] != b[0]:
            neighbors[a[0]].add(b[0])
            neighbors[b[0]].add(a[0])
    result, geometry = {}, {}
    for pid, part in parts.items():
        text = ' '.join([part['part'], *part['volume_zones']]).casefold()
        role = next((role for token, role in (
            ('impeller', 'impeller'), ('rotor', 'impeller'), ('volute', 'volute'),
            ('seal', 'seal'), ('stator', 'stationary'), ('stationary', 'stationary'),
            ('inlet_body', 'stationary')) if token in text), None)
        cloud = np.concatenate([f['points'] for key, f in faces.items()
                                if key[0] == pid and len(f['points'])]) if any(
                                    key[0] == pid and len(f['points']) for key, f in faces.items()) else np.zeros((1, 3))
        center = cloud.mean(axis=0)
        _, sigma, axes = np.linalg.svd(cloud-center, full_matrices=False)
        if len(sigma) < 3:
            sigma, axes = np.ones(3), np.eye(3)
        spans = np.ptp((cloud-center)@axes.T, axis=0)
        order = np.argsort(spans)[::-1]
        spans, axes = spans[order], axes[order]
        geometry[pid] = dict(center=center, spans=spans, axis=axes[-1],
                             degree=len(neighbors[pid]))
        if role:
            result[pid] = dict(role=role, uncertain=False,
                reason='optional part/volume name hint; no exact original selection required')
    unknown = [pid for pid in parts if pid not in result]
    if unknown and not any(r['role'] == 'impeller' for r in result.values()):
        # A broad, relatively thin, near round body with several neighbors is a
        # rotating-region candidate. This remains a heuristic, not physical proof.
        def rotor_score(pid):
            g = geometry[pid]
            a, b, c = np.maximum(g['spans'], max(float(max(g['spans']))*1e-10, 1e-300))
            largest = max(max(g['spans']) for g in geometry.values())
            if b < 0.35*largest:
                return 0.0
            return (b/a)*(b/c)**2*(1+g['degree'])*math.sqrt(a/max(largest, 1e-300))
        ranked = sorted(unknown, key=rotor_score, reverse=True)
        pid = ranked[0]
        g = geometry[pid]
        if rotor_score(pid) > 0 and g['spans'][1] > 0.55*g['spans'][0] and g['spans'][2] < 0.65*g['spans'][1]:
            result[pid] = dict(role='impeller', uncertain=True,
                reason='broad flattened near-round body and region contact degree')
    rotors = [pid for pid, r in result.items() if r['role'] == 'impeller']
    remaining = [pid for pid in parts if pid not in result]
    if rotors and remaining and not any(r['role'] == 'volute' for r in result.values()):
        candidates = [pid for pid in remaining if any(pid in neighbors[r] for r in rotors)]
        if candidates:
            pid = max(candidates, key=lambda p: float(np.prod(geometry[p]['spans'])))
            result[pid] = dict(role='volute', uncertain=True,
                reason='largest bounding-span volume among bodies contacting rotating-region candidate')
    for pid in parts:
        if pid in result:
            continue
        a, b, c = geometry[pid]['spans']
        thin = c < 0.15*max(b, 1e-300) and any(pid in neighbors[r] for r in rotors)
        result[pid] = dict(role='seal' if thin else 'stationary', uncertain=True,
            reason='thin adjacent body suggests seal' if thin else
                   'remaining fluid candidate; geometry cannot uniquely prove material or region role')
    return result, geometry


def classify_boundary_faces(parts, faces, cfg, deadline=None):
    """Pure geometry classifier. Every fluid CAD face receives exactly one zone."""
    import numpy as np
    all_points = [f['points'] for f in faces.values() if len(f['points'])]
    cloud = np.concatenate(all_points) if all_points else np.zeros((1, 3))
    diagonal = float(np.linalg.norm(np.ptp(cloud, axis=0)))
    tolerance = max(diagonal*cfg['recognition_contact_ratio'], np.finfo(float).eps*max(diagonal, 1e-300)*32)
    keys = sorted(faces)
    candidates = {k: [] for k in keys}
    for i, a in enumerate(keys):
        recognition_checkpoint(deadline)
        f = faces[a]
        if not len(f['triangles']):
            continue
        for b in keys[i+1:]:
            g = faces[b]
            if a[0] == b[0] or not len(g['triangles']):
                continue
            if np.any(f['low'] > g['high']+tolerance) or np.any(g['low'] > f['high']+tolerance):
                continue
            candidates[a].append(b)
            candidates[b].append(a)
    coverage, contacts = {}, set()
    for key in keys:
        recognition_checkpoint(deadline)
        coverage[key] = contact_coverage(faces[key], [faces[k] for k in candidates[key]],
                                        tolerance, cfg['recognition_samples'], deadline=deadline)
    fully_covered = {k for k in keys if coverage[k] >= cfg['recognition_contact_coverage']}
    for a in fully_covered:
        for b in candidates[a]:
            if b in fully_covered:
                # Reject bbox-only overlaps: each pair must actually touch.
                if contact_coverage(faces[a], [faces[b]], tolerance,
                                    cfg['recognition_samples'], deadline=deadline) > 0:
                    contacts.add(tuple(sorted((a, b))))
    regions, region_geometry = infer_regions(parts, faces, contacts)
    assignments = {}
    # Keep the two sides separately named for Fluent pairing, never stitch them.
    adjacency = {k: set() for k in fully_covered}
    for a, b in contacts:
        adjacency[a].add(b)
        adjacency[b].add(a)
    seen, pair_id, zone_id = set(), 0, 0
    for key in sorted(adjacency):
        if key in seen or not adjacency[key]:
            continue
        pending, component = [key], set()
        while pending:
            node = pending.pop()
            if node in component:
                continue
            component.add(node)
            pending.extend(adjacency[node]-component)
        seen.update(component)
        pair_id += 1
        for pid in sorted({k[0] for k in component}):
            zone_id += 1
            for k in component:
                if k[0] == pid:
                    assignments[k] = dict(zone=f'auto_interface_{zone_id}', type='interface',
                        uncertain=True, interface_pair=pair_id,
                        reason=f'cross-body tessellated contact; coverage={coverage[k]:.6g}; '
                               f'tolerance={tolerance:.6g}; separate interface side, pairing required')
    # Terminal cap candidates are considered only AFTER all contacts are reserved.
    port_candidates = []
    for key in keys:
        if key in assignments:
            continue
        f = faces[key]
        if not len(f['points']) or regions[key[0]]['role'] in ('impeller', 'seal'):
            continue
        if not (f['planarity'] <= cfg['recognition_planarity_ratio'] and
                f['normal_coherence'] >= 0.9 and f['planar_aspect'] >= 0.6 and
                f['circular_radius_variation'] <= 0.12):
            continue
        body = np.concatenate([g['points'] for k, g in faces.items()
                               if k[0] == key[0] and len(g['points'])])
        projection = (body-f['center'])@f['normal']
        terminal = min(abs(float(projection.min())), abs(float(projection.max()))) <= max(tolerance, diagonal*cfg['recognition_planarity_ratio'])
        if not terminal:
            continue
        # The adjoining tube wall should extend inward along the cap normal.
        adjacent = [faces[(key[0], fid)] for fid in f.get('adjacent_faces', [])
                    if (key[0], fid) in faces and len(faces[(key[0], fid)]['points'])]
        alignments = []
        for g in adjacent:
            delta = f['center']-g['center']
            length = float(np.linalg.norm(delta))
            if length > tolerance:
                alignments.append(abs(float(delta@f['normal']))/length)
        axis_alignment = max(alignments, default=0.0)
        if axis_alignment < 0.6:
            continue
        port_candidates.append((key, axis_alignment, f['area']))
    rotor_ids = [p for p, r in regions.items() if r['role'] == 'impeller']
    inlet_axis = region_geometry[rotor_ids[0]]['axis'] if rotor_ids else None
    def inlet_score(item):
        key, alignment, area = item
        role = regions[key[0]]['role']
        axial = abs(float(faces[key]['normal']@inlet_axis)) if inlet_axis is not None else 0.0
        return (role == 'stationary', round(axial, 8), round(alignment, 8),
                round(area/max(diagonal*diagonal, 1e-300), 8), key)
    if port_candidates:
        chosen = max(port_candidates, key=inlet_score)
        key = chosen[0]
        assignments[key] = dict(zone='auto_inlet', type='inlet', uncertain=True,
            interface_pair=None, reason='outer planar circular/annular cap; adjoining wall axis '
            'aligned; stationary/rotor-axis preference; flow direction inferred, verify in Fluent')
        rest = [c for c in port_candidates if c[0] != key]
        if rest:
            chosen = max(rest, key=lambda item: (regions[item[0][0]]['role'] == 'volute',
                                                 round(item[1], 8), round(item[2]/max(diagonal*diagonal, 1e-300), 8), item[0]))
            assignments[chosen[0]] = dict(zone='auto_outlet', type='outlet', uncertain=True,
                interface_pair=None, reason='remaining outer planar circular/annular cap; '
                'adjoining wall axis aligned; volute preference; verify flow direction in Fluent')
    for key in keys:
        if key not in assignments:
            role = regions[key[0]]['role']
            assignments[key] = dict(zone=f'auto_{role}_wall', type='wall', uncertain=True,
                interface_pair=None, reason='conservative remaining external face; '+
                ('incomplete/partial contact or unavailable facets; ' if coverage[key] > 0 or not len(faces[key]['triangles']) else '')+
                regions[key[0]]['reason'])
    return assignments, regions, dict(contact_tolerance=tolerance,
        port_candidates=len(port_candidates), interface_pairs=pair_id,
        contact_coverage={f'{k[0]}:{k[1]}': v for k, v in coverage.items()})


@timed_stage('CAD display facets')
def read_cad_faces(model):
    """Read geometry-display facets (also available before surface meshing)."""
    import numpy as np
    faces = {}
    for p in model.parts:
        labels = {n: face_ids(model, p, n) for n in cached_query(p, 'get_labels')}
        for fid in map(int, cached_query(p, 'get_topo_faces')):
            faces[(p.id, fid)] = dict(points=np.empty((0, 3)), triangles=np.empty((0, 3, 3)),
                area=None, original_labels=[n for n, fs in labels.items() if fid in fs],
                adjacent_faces=list(map(int, cached_query(model.topo_data, 'get_adjacent_topo_faces_of_topo_faces', [fid]))),
                measurement_status='no_display_facets_default_wall')
    try:
        result = prime.MeshInfo(model).get_face_and_edge_connectivity(
            [p.id for p in model.parts], prime.FaceAndEdgeConnectivityParams(model))
        if result.error_code != prime.ErrorCode.NOERROR:
            raise RuntimeError(str(result.error_code))
        for pid, data in zip(result.part_ids, result.face_connectivity_result_per_part):
            offset, cell_offset = 0, 0
            for i, fid in enumerate(data.topo_face_ids):
                count = int(data.num_nodes_per_face_zonelet[i])
                ncells = int(data.num_face_list_per_face_zonelet[i])
                points = data.node_coords[offset:offset+3*count]
                cells = data.face_list[cell_offset:cell_offset+ncells]
                offset += 3*count
                cell_offset += ncells
                key = (int(pid), int(fid))
                if key not in faces:
                    continue
                try:
                    descriptor = facet_descriptor(points, cells)
                    faces[key].update(descriptor)
                except (ValueError, IndexError, ArithmeticError) as exc:
                    LOG.warning('Face %s facets unavailable: %s; default wall', key, exc)
    except Exception as exc:
        LOG.warning('CAD display geometry query unavailable: %s; unknown faces default wall', exc)
    LOG.info('CAD display facets measured for %s/%s faces',
             sum(bool(len(f['triangles'])) for f in faces.values()), len(faces))
    return faces


@timed_stage('automatic boundary recognition')
def inspect_names(model, output, cfg=None):
    """Historical entry name; now performs automatic recognition, never name validation."""
    global BOUNDARIES, WALLS, AUTO_FACE_SCOPES, AUTO_MAPPING, AUTO_RECOGNITION
    cfg = cfg or DEFAULTS
    BOUNDARIES, WALLS, AUTO_FACE_SCOPES = (), (), {}
    invalidate_queries()
    original = inventory(model)
    excluded = []
    for p in original:
        # Explicit solid material evidence wins; arbitrary labels are NOT used as
        # material evidence because generic imported labels can span many bodies.
        names = [p['part'], *p['volume_zones']]
        if any(n.casefold() == 'solid' for n in names):
            excluded.append(p['part_id'])
    if excluded:
        checked(model.delete_parts(excluded), 'exclude explicit solid CAD bodies')
        invalidate_queries()
    if not model.parts:
        raise ValueError('No fluid candidate bodies remain after excluding solids')
    parts = {p['part_id']: p for p in inventory(model)}
    faces = read_cad_faces(model)
    if not faces:
        raise RuntimeError('Imported CAD has no topology faces to mesh')
    try:
        assignments, regions, details = classify_boundary_faces(parts, faces, cfg,
            deadline=time.monotonic()+cfg['recognition_budget_seconds'])
    except RecognitionBudgetExceeded:
        LOG.warning('Automatic recognition CPU budget exceeded; all faces fall back to uncertain wall')
        regions, _ = infer_regions(parts, faces, [])
        assignments = {key: dict(zone='auto_'+regions[key[0]]['role']+'_wall', type='wall',
            uncertain=True, interface_pair=None, reason='recognition CPU budget exceeded; conservative wall fallback')
            for key in faces}
        details = dict(recognition_budget_exceeded=True, port_candidates=0, interface_pairs=0)
    AUTO_MAPPING = []
    for key, assignment in sorted(assignments.items()):
        pid, fid = key
        AUTO_FACE_SCOPES.setdefault((pid, assignment['zone']), set()).add(fid)
        f = faces[key]
        row = dict(original_label_or_id=';'.join(f['original_labels']) or f'topo_face:{fid}',
            original_topo_face_id=fid, original_part_id=pid, zone_name=assignment['zone'],
            zone_type=assignment['type'], body_region=regions[pid]['role'],
            original_body=parts[pid]['part'], original_volume_zones=';'.join(parts[pid]['volume_zones']),
            face_count=1, area=f['area'], area_status=f['measurement_status'],
            centroid=json.dumps(f['center'].tolist()) if 'center' in f else '',
            plane_normal=json.dumps(f['normal'].tolist()) if 'normal' in f else '',
            planarity=f.get('planarity'), normal_coherence=f.get('normal_coherence'),
            uncertain=assignment['uncertain'] or regions[pid]['uncertain'],
            recognition_basis=assignment['reason'], interface_pair=assignment['interface_pair'])
        AUTO_MAPPING.append(row)
    BOUNDARIES = tuple(sorted({r['zone_name'] for r in AUTO_MAPPING}))
    WALLS = tuple(sorted({r['zone_name'] for r in AUTO_MAPPING if r['zone_type'] == 'wall'}))
    AUTO_RECOGNITION = dict(regions=regions, excluded_solid_part_ids=excluded,
                            details=details, notes=[
        'Unknown material bodies retained as uncertain fluid candidates; geometry alone cannot prove fluid vs solid.',
        'Approximate facet areas use imported CAD units squared; no computational mesh has been generated.',
        'Partial-contact unsplit faces default to uncertain wall; inspect mapping before solving.',
        'Geometric interface faces have separate zones; Fluent pairing is not automatically configured.'])
    write_mapping(output)
    if not {'auto_inlet', 'auto_outlet'}.issubset(BOUNDARIES):
        LOG.warning('No confident pair of terminal ports; unmatched faces remain uncertain wall')
    LOG.info('Automatic regions: %s; boundary faces=%s; zones=%s; solids excluded=%s',
             regions, len(AUTO_MAPPING), BOUNDARIES, excluded)
    return original, {pid: r['role'] for pid, r in regions.items()}


def write_mapping(output):
    if AUTO_MAPPING:
        with (output/'auto_boundary_mapping.csv').open('w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=list(AUTO_MAPPING[0]))
            writer.writeheader()
            writer.writerows(AUTO_MAPPING)


@timed_stage('boundary zone assignment')
def prepare_boundaries(model, coarse=False):
    """Assign each CAD face once, tolerating server-suggested name collisions."""
    global BOUNDARIES, WALLS, AUTO_FACE_SCOPES
    renamed, new_scopes = {}, {}
    old_walls = set(WALLS)
    for name in BOUNDARIES:
        zone = checked(model.create_zone(name, prime.ZoneType.FACE), 'create automatic '+name).zone_id
        actual = cached_query(model, 'get_zone_name', zone)
        renamed[name] = actual
        for p in model.parts:
            fs = sorted(AUTO_FACE_SCOPES.get((p.id, name), set()))
            if not fs:
                continue
            checked(p.remove_zone_on_topo_entities(fs), 'replace old face zone membership')
            checked(p.add_topo_entities_to_zone(zone, fs), 'assign automatic '+actual)
            if not coarse:
                checked(p.add_labels_on_topo_entities(['__pump_all', '__pump_'+actual], fs), 'auto sizing labels')
            new_scopes[(p.id, actual)] = set(fs)
    # Update all aliases simultaneously: a suggested name may equal a future
    # requested name, so in-place sequential replacement would lose scopes.
    for row in AUTO_MAPPING:
        if row['zone_name'] in renamed:
            row['zone_name'] = renamed[row['zone_name']]
    AUTO_FACE_SCOPES = new_scopes
    BOUNDARIES = tuple(renamed.values())
    WALLS = tuple(renamed[n] for n in old_walls)
    invalidate_queries()
    if coarse:
        return
    for p in model.parts:
        fs = list(map(int, cached_query(p, 'get_topo_faces')))
        if '__pump_wall' in cached_query(p, 'get_labels'):
            checked(p.remove_labels_from_topo_entities(['__pump_wall'], fs), 'reset prism scope')
        wall_faces = sorted({fid for n in WALLS for fid in AUTO_FACE_SCOPES.get((p.id, n), set())})
        if wall_faces:
            checked(p.add_labels_on_topo_entities(['__pump_wall'], wall_faces), 'exclusive auto wall prism scope')
    invalidate_queries()


@timed_stage('boundary validation')
def verify_auto_boundaries(model, coarse=False):
    for p in model.parts:
        actual = set(map(int, cached_query(p, 'get_topo_faces')))
        assigned = [fid for (pid, _), fs in AUTO_FACE_SCOPES.items() if pid == p.id for fid in fs]
        if set(assigned) != actual or len(assigned) != len(actual):
            raise RuntimeError(f'{p.name}: automatic assignment coverage changed during meshing')
        for name in (() if coarse else BOUNDARIES):
            expected = AUTO_FACE_SCOPES.get((p.id, name), set())
            if expected:
                observed = set(map(int, p.get_topo_faces_of_zone_name_pattern(name, prime.NamePatternParams(model))))
                if observed != expected:
                    raise RuntimeError(f'{p.name}: auto zone membership changed: {name}')
    return inventory(model)


def bbox(model, zonelets):
    if not zonelets:
        raise ValueError('Empty geometry zonelets')
    b = prime.SurfaceUtilities(model).get_bounding_box_of_zonelets(list(zonelets))
    low, high = [b.xmin, b.ymin, b.zmin], [b.xmax, b.ymax, b.zmax]
    if not all(math.isfinite(v) for v in low + high):
        raise ValueError('Nonfinite geometry bounds')
    return {'low': low, 'high': high, 'extents': [v-u for u, v in zip(low, high)]}


@timed_stage('geometry measurement')
def measure(model, coarse=False):
    all_z = []
    measured = {}
    for p in model.parts:
        fs = list(cached_query(p, 'get_topo_faces'))
        zs = list(cached_query(model.topo_data, 'get_geom_zonelets_of_topo_faces', fs))
        all_z.extend(zs)
        part = bbox(model, zs)
        spans = []
        # Use each edge's diagonal, never its smallest Cartesian extent: a
        # tilted planar face's bbox thickness is not a physical clearance.
        for e in (() if coarse else cached_query(p, 'get_topo_edges')):
            ez = list(cached_query(model.topo_data, 'get_geom_zonelets_of_topo_edges', [int(e)]))
            if ez:
                ext = bbox(model, ez)['extents']
                length = math.sqrt(sum(v*v for v in ext))
                if length > 0:
                    spans.append(length)
        part['edge_bbox_diagonals'] = spans
        part['boundaries'] = {}
        for n in (() if coarse else BOUNDARIES):
            faces = face_ids(model, p, n)
            if faces:
                z = list(cached_query(model.topo_data, 'get_geom_zonelets_of_topo_faces', sorted(faces)))
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



@timed_stage('coarse global sizing and meshing')
def mesh_coarse(model, sizes, cfg, members):
    h = sizes['base']
    # This is the ONE global sizing control. Zero SizeControl objects, zero
    # soft/curvature/proximity setters, zero volumetric size-field computations.
    with phase('coarse: set single global sizing'):
        checked(model.set_global_sizing_params(prime.GlobalSizingParams(
            model, min=h, max=h, growth_rate=cfg['growth_rate'])), 'coarse global sizing')
    for p in model.parts:
        with phase('coarse surface_mesh '+p.name):
            checked(prime.Surfer(model).mesh_topo_faces(p.id,
                list(cached_query(p, 'get_topo_faces')), prime.SurferParams(model,
                    size_field_type=prime.SizeFieldType.CONSTANT, constant_size=h,
                    generate_quads=False)), 'coarse surface mesh '+p.name)
            summary = p.get_summary(prime.PartSummaryParams(model))
            if summary.n_unmeshed_topo_faces or summary.n_faces <= 0:
                raise RuntimeError(f'{p.name}: coarse surface mesh incomplete: {summary}')
    invalidate_queries()
    fills = {}
    for p in model.parts:
        with phase('coarse volume_mesh '+p.name):
            if not len(cached_query(p, 'get_topo_volumes')):
                checked(p.compute_topo_volumes(prime.ComputeVolumesParams(model)), 'coarse closed volumes')
                invalidate_queries()
            if not len(cached_query(p, 'get_topo_volumes')):
                raise RuntimeError(p.name+': no closed fluid volume')
            if not len(cached_query(p, 'get_volume_zones')):
                z = checked(model.create_zone('auto_'+members[p.id]+'_fluid_'+str(p.id),
                    prime.ZoneType.VOLUME), 'coarse fluid zone').zone_id
                checked(p.add_topo_entities_to_zone(z, cached_query(p, 'get_topo_volumes')), 'coarse fluid membership')
            vc = model.control_data.create_volume_control()
            vc.set_params(prime.VolumeControlParams(model, cell_zonelet_type=prime.CellZoneletType.FLUID))
            checked(vc.set_scope(scope(model, p.name, volume=True)), 'coarse fluid scope')
            checked(prime.AutoMesh(model).mesh(p.id, prime.AutoMeshParams(model,
                volume_fill_type=prime.VolumeFillType.TET, max_size=h,
                size_field_type=prime.SizeFieldType.CONSTANT,
                volume_control_ids=[vc.id], prism_control_ids=[])), 'coarse TET volume '+p.name)
            summary = p.get_summary(prime.PartSummaryParams(model))
            if summary.n_cells <= 0:
                raise RuntimeError(p.name+': no coarse volume cells')
            fills[p.id] = 'TET'
    invalidate_queries()
    return fills


def coarse_sizing(geometry, cfg, iteration):
    d = math.sqrt(sum(v*v for v in geometry['overall']['extents']))
    if not math.isfinite(d) or d <= 0:
        raise ValueError('Invalid geometry diagonal')
    h = d*cfg['coarse_diagonal_ratio']*cfg['refinement_factor']**iteration
    result = dict(mode='coarse', diagonal=d, base=h, growth_rate=cfg['growth_rate'],
                  global_control_count=1, size_control_object_count=0,
                  local_controls=0, curvature=False, proximity=False,
                  prism_layers=0, length_basis='overall bbox diagonal * coarse_diagonal_ratio * refinement_factor**iteration')
    LOG.info('Coarse sizing: %s', result)
    return result


@timed_stage('refined mesh')
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
            local = s['local'] if n in WALLS or n.startswith('auto_interface') else min(
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
            if not len(cached_query(p, 'get_topo_volumes')):
                checked(p.compute_topo_volumes(prime.ComputeVolumesParams(model)), 'closed fluid volumes')
            if not len(cached_query(p, 'get_topo_volumes')):
                raise ValueError(f'{p.name}: no closed fluid volume')
            if not len(cached_query(p, 'get_volume_zones')):
                name = 'auto_'+members[p.id]+'_fluid_'+str(p.id)
                z = checked(model.create_zone(name, prime.ZoneType.VOLUME), 'fluid '+name).zone_id
                checked(p.add_topo_entities_to_zone(z, cached_query(p, 'get_topo_volumes')), 'fluid membership')
            vc = model.control_data.create_volume_control()
            vc.set_params(prime.VolumeControlParams(model, cell_zonelet_type=prime.CellZoneletType.FLUID))
            checked(vc.set_scope(scope(model, p.name, volume=True)), 'fluid volume scope')
            prism_ids = []
            if '__pump_wall' in cached_query(p, 'get_labels'):
                pc = model.control_data.create_prism_control()
                checked(pc.set_surface_scope(scope(model, p.name, '__pump_wall')), 'wall-only prism')
                checked(pc.set_volume_scope(scope(model, p.name, volume=True)), 'prism volume')
                pc.set_growth_params(prime.PrismControlGrowthParams(model,
                    offset_type=prime.PrismControlOffsetType.UNIFORM,
                    n_layers=s['layer_count_requested'], growth_rate=s['prism_growth'],
                    first_height=s['first_height_requested']))
                prism_ids.append(pc.id)
            preferred = cfg['preferred_fill'] if members[p.id] == 'volute' else 'TET'
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


@timed_stage('volume quality')
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


@timed_stage('quality smoothing')
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



@timed_stage('Fluent export')
def export_fluent_mesh(model, output, cfg):
    raw = output/'pump.msh'
    checked(prime.FileIO(model).export_fluent_meshing_mesh(str(raw),
        prime.ExportFluentMeshingMeshParams(model)), 'export native Fluent MSH')
    if not raw.is_file() or raw.stat().st_size == 0:
        raise RuntimeError('Prime export is empty')
    try:
        return convert_hdf5(raw, output, cfg)
    except (RuntimeError, OSError, ImportError, subprocess.SubprocessError) as exc:
        LOG.warning('Optional HDF5 conversion failed: %s; native Fluent MSH retained. '
                    'Review/set boundary types from mapping in Fluent.', exc)
        return raw


def convert_hdf5(raw, output, cfg):
    """Optional local Fluent conversion; no fake HDF5 renaming."""
    fluent = cfg['fluent_executable'] or shutil.which('fluent')
    if not fluent:
        root = os.environ.get('AWP_ROOT241')
        if root:
            candidate = Path(root)/'fluent'/'ntbin'/'win64'/'fluent.exe'
            if candidate.is_file():
                fluent = str(candidate)
    if not fluent:
        LOG.warning('Fluent executable not found; exported native Fluent-readable pump.msh. '
                    'Automatic types are in auto_boundary_mapping.csv; set them in Fluent.')
        return raw
    import h5py
    final = output / 'pump.msh.h5'
    candidate = output / 'pump_candidate.msh.h5'
    candidate.unlink(missing_ok=True)
    def quote(path):
        s = str(path).replace('\\', '/')
        if any(ch in s for ch in ('"', '\n', '\r')):
            raise ValueError('Journal path contains unsupported quote/newline')
        return '"'+s+'"'
    journal = output / 'convert_to_hdf5.jou'
    types = {'inlet': 'velocity-inlet', 'outlet': 'pressure-outlet',
             'interface': 'interface', 'wall': 'wall'}
    zone_types = {r['zone_name']: types[r['zone_type']] for r in AUTO_MAPPING}
    commands = ''.join('/define/boundary-conditions/zone-type '+name+' '+kind+'\n'
                       for name, kind in sorted(zone_types.items()))
    journal.write_text('/file/read-mesh '+quote(raw)+'\n'+commands+
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
    p.add_argument('--coarse', action='store_true', help='constant global sizing, TET, no local controls or prisms')
    p.add_argument('--prime-timeout', type=float, default=900,
                   help='diagnostic threshold per Prime call/stage in seconds; does not cancel server')
    p.add_argument('--cad', type=Path, default=Path('./FFF-8.scdoc'))
    p.add_argument('--output', type=Path, default=Path('./output'))
    p.add_argument('--target-orthogonal-quality', type=float, default=0.1)
    p.add_argument('--max-iterations', type=int, default=4, help='maximum remesh repairs after initial attempt')
    p.add_argument('--mesh-format', choices=['Fluent', 'fluent'], default='Fluent')
    p.add_argument('--dry-run', action='store_true', help='LOCAL Prime import/geometry/automatic boundary recognition only')
    p.add_argument('--report', type=Path)
    p.add_argument('--config', type=Path)
    args = p.parse_args(argv)
    if not 0.1 <= args.target_orthogonal_quality < 1:
        p.error('--target-orthogonal-quality must be >= 0.1 and < 1')
    if not math.isfinite(args.prime_timeout) or args.prime_timeout <= 0:
        p.error('--prime-timeout must be finite and positive')
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
    for key in ('coarse_diagonal_ratio', 'global_diagonal_ratio', 'minimum_global_ratio', 'local_span_ratio',
                'wall_global_ratio', 'port_global_ratio', 'refinement_factor',
                'prism_first_to_local_ratio', 'prism_total_to_local_ratio',
                'max_skewness', 'min_element_quality', 'recognition_contact_ratio',
                'recognition_contact_coverage', 'recognition_planarity_ratio'):
        if cfg[key] >= 1:
            raise ValueError('Ratio must be < 1: '+key)
    if not (1 < cfg['growth_rate'] <= 1.3 and 1 < cfg['prism_growth_rate'] <= 1.3):
        raise ValueError('Growth rates must lie in (1, 1.3]')
    for key in ('prism_max_layers', 'elements_per_gap', 'fluent_timeout_seconds', 'recognition_samples'):
        if not isinstance(cfg[key], int):
            raise ValueError('Expected integer: '+key)
    if cfg['preferred_fill'] not in ('POLY', 'HEXCOREPOLY', 'TET'):
        raise ValueError('preferred_fill must be POLY, HEXCOREPOLY or TET')
    return cfg


@timed_stage('import CAD')
def import_geometry(model, cad):
    return checked(prime.FileIO(model).import_cad(str(cad.resolve()),
        prime.ImportCadParams(model, geometry_transfer=True,
                              part_creation_type=prime.PartCreationType.BODY)), 'import CAD')


def run(argv=None):
    global prime, MONITOR
    args = arguments(argv)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Do not allow stale output from an earlier successful run to masquerade as this run.
    if any((output/name).exists() for name in ('pump.msh.h5', 'pump.msh')):
        raise FileExistsError('Use a fresh output directory; a final mesh already exists')
    handlers = [logging.StreamHandler(), logging.FileHandler(output/'meshing_log.txt', encoding='utf-8')]
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format='%(asctime)s %(levelname)s %(message)s', force=True)
    report_path = args.report.resolve() if args.report else output/'mesh_quality_report.json'
    report = {'status': 'started', 'cad': str(args.cad.resolve()), 'attempts': [],
              'limitations': LIMITATIONS, 'validation': 'local execution required',
              'precise_feature_volume_statistics': None, 'actual_prism_thickness': None}
    report['mode'] = 'coarse' if args.coarse else 'refined'
    report['timeout_policy'] = 'diagnostic only; no unsafe concurrent Prime cancellation/retry'
    MONITOR = RuntimeMonitor(output, args.prime_timeout)
    MONITOR.start()
    try:
        save_report(report, report_path, output)
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
            with phase('Prime session/startup'), launch_prime() as client, observe_prime_calls(client.model):
                model = client.model
                invalidate_queries()
                import_geometry(model, args.cad)
                original, members = inspect_names(model, output, cfg)
                report['automatic_recognition'] = AUTO_RECOGNITION
                report['automatic_boundary_mapping'] = AUTO_MAPPING
                geometry = measure(model, coarse=args.coarse)
                report['geometry'] = geometry
                report['original_names'] = original
                if args.dry_run:
                    report['status'] = 'dry_run_automatic_recognition_completed'
                    return 0
                # IDs may differ on reimport: use exact part names to carry failure scope.
                failed_ids = {p.id for p in model.parts if p.name in failed_parts}
                sizes = coarse_sizing(geometry, cfg, iteration) if args.coarse else sizing(geometry, cfg, iteration, failed_ids)
                attempt['requested_sizing'] = sizes
                save_report(report, report_path, output)
                prepare_boundaries(model, coarse=args.coarse)
                write_mapping(output)
                try:
                    attempt['fill_types'] = (mesh_coarse(model, sizes, cfg, members) if args.coarse
                                             else mesh(model, sizes, cfg, members, force_tet))
                except RuntimeError as exc:
                    attempt['meshing_error'] = str(exc)
                    if not args.coarse and not force_tet and iteration < args.max_iterations:
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
                attempt['named_surface_quality'] = [] if args.coarse else surface_diagnostics(model)
                report['final_names'] = verify_auto_boundaries(model, coarse=args.coarse)
                if not failed:
                    report['mesh'] = str(export_fluent_mesh(model, output, cfg))
                    report['boundary_type_application'] = (
                        'requested_by_optional_Fluent_journal_verify_locally'
                        if report['mesh'].endswith('.h5') else
                        'intended_types_in_mapping_set_or_verify_in_Fluent')
                    report['interface_pairing'] = 'not_configured_use_interface_pair_mapping'
                    report['status'] = 'part_quality_passed_with_documented_diagnostic_limitations'
                    report['validation'] = 'Prime part-level checks passed; Fluent inspection still required'
                    LOG.info('Export completed; this is not certification of unsupported local diagnostics')
                    return 0
                failed_parts = {p.name for p in model.parts if p.id in failed}
                LOG.warning('Reimport/remesh failed parts with reduced sizing and prism height: %s', failed_parts)
            save_report(report, report_path, output)
        raise RuntimeError('Quality repair limit exhausted; no final Fluent mesh exported')
    except Exception as exc:
        report['status'] = 'failed'
        report['error'] = str(exc)
        LOG.exception('Meshing workflow failed')
        return 2
    finally:
        report['last_runtime_state'] = MONITOR.snapshot()
        MONITOR.stop()
        MONITOR = None
        save_report(report, report_path, output)


if __name__ == '__main__':
    raise SystemExit(run())
