"""Offline analytic-geometry tests; no Prime server or CAD is opened."""
import csv
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import pump_mesh_auto as m


def face(points, cells, adjacent=()):
    result = m.facet_descriptor(points, cells)
    result.update(original_labels=['label0:generic'], adjacent_faces=list(adjacent))
    return result


def square(z=0, x0=0, x1=1):
    return face([[x0, 0, z], [x1, 0, z], [x1, 1, z], [x0, 1, z]],
                [3, 0, 1, 2, 3, 0, 2, 3])


def pipe():
    n = 32
    ring = np.array([[math.cos(t), math.sin(t), 0] for t in np.arange(n)*2*math.pi/n])
    points = np.concatenate((ring, ring+[0, 0, 6]))
    wall = []
    for i in range(n):
        j = (i+1) % n
        wall.extend([4, i, j, n+j, n+i])
    facets = {(1, 1): face(ring, [n, *range(n)], [3]),
              (1, 2): face(ring+[0, 0, 6], [n, *range(n)], [3]),
              (1, 3): face(points, wall, [1, 2])}
    return facets


def parts(*names):
    return {i+1: dict(part=name, volume_zones=[], labels=['label0:generic']) for i, name in enumerate(names)}


class AutoGeometryTests(unittest.TestCase):
    def classify(self, bodies, faces):
        return m.classify_boundary_faces(bodies, faces, m.DEFAULTS)

    def test_area_normal_and_annular_shape(self):
        descriptor = square()
        self.assertAlmostEqual(descriptor['area'], 1)
        self.assertAlmostEqual(abs(descriptor['normal'][2]), 1)
        self.assertEqual(descriptor['planarity'], 0)
        n = 32
        outer = np.array([[math.cos(t), math.sin(t), 0] for t in np.arange(n)*2*math.pi/n])
        points = np.concatenate((outer, outer/2))
        cells = []
        for i in range(n):
            j = (i+1) % n
            cells.extend([4, i, j, j+n, i+n])
        annulus = face(points, cells)
        self.assertLess(annulus['circular_radius_variation'], 1e-12)
        self.assertAlmostEqual(annulus['area'], 0.75*math.pi, delta=0.02)

    def test_exact_point_triangle_distances(self):
        triangles = np.array([[[0, 0, 0], [1, 0, 0], [0, 1, 0]]])
        distances = m.surface_distance([[0.2, 0.2, 2], [2, 0, 0], [-1, -1, 0]], triangles)
        np.testing.assert_allclose(distances, [2, 1, math.sqrt(2)])

    def test_contact_precedes_ports_and_has_separate_sides(self):
        faces = {(1, 1): square(), (2, 2): square()}
        assigned, _, details = self.classify(parts('renamed_a', 'renamed_b'), faces)
        self.assertEqual({r['type'] for r in assigned.values()}, {'interface'})
        self.assertEqual(len({r['zone'] for r in assigned.values()}), 2)
        self.assertEqual(len({r['interface_pair'] for r in assigned.values()}), 1)
        self.assertEqual(details['interface_pairs'], 1)

    def test_different_face_partition_contact(self):
        faces = {(1, 1): square(), (2, 2): square(x1=0.5), (2, 3): square(x0=0.5)}
        assigned, _, _ = self.classify(parts('unknown_a', 'unknown_b'), faces)
        self.assertEqual({r['type'] for r in assigned.values()}, {'interface'})
        self.assertEqual(len({r['interface_pair'] for r in assigned.values()}), 1)

    def test_parallel_gap_not_interface(self):
        assigned, _, _ = self.classify(parts('a', 'b'), {(1, 1): square(), (2, 2): square(z=0.01)})
        self.assertTrue(all(r['type'] == 'wall' for r in assigned.values()))
        self.assertTrue(all(r['uncertain'] for r in assigned.values()))

    def test_overlapping_bbox_alone_is_not_contact(self):
        a = face([[0, 0, 0], [1, 0, 0], [0, 1, 0]], [3, 0, 1, 2])
        b = face([[1, 1, 0], [0.6, 1, 0], [1, 0.6, 0]], [3, 0, 1, 2])
        assigned, _, _ = self.classify(parts('a', 'b'), {(1, 1): a, (2, 2): b})
        self.assertTrue(all(r['type'] == 'wall' for r in assigned.values()))

    def test_partial_contact_defaults_uncertain_wall(self):
        assigned, _, _ = self.classify(parts('a', 'b'), {(1, 1): square(), (2, 2): square(x1=0.5)})
        self.assertTrue(all(r['type'] == 'wall' for r in assigned.values()))
        self.assertTrue(all(r['uncertain'] for r in assigned.values()))

    def test_missing_facets_and_names_do_not_leave_unassigned_faces(self):
        empty = dict(points=np.empty((0, 3)), triangles=np.empty((0, 3, 3)), area=None)
        assigned, _, _ = self.classify(parts('label0:xxx'), {(1, 99): empty})
        self.assertEqual(set(assigned), {(1, 99)})
        self.assertEqual(assigned[(1, 99)]['type'], 'wall')
        self.assertTrue(assigned[(1, 99)]['uncertain'])

    def test_terminal_caps_and_remaining_wall(self):
        assigned, _, _ = self.classify(parts('totally_renamed_body'), pipe())
        self.assertCountEqual([r['type'] for r in assigned.values()], ['inlet', 'outlet', 'wall'])
        self.assertEqual(assigned[(1, 3)]['type'], 'wall')
        self.assertTrue(all(r['uncertain'] for r in assigned.values()))

    def test_rotation_and_unit_invariance(self):
        original = pipe()
        assigned, _, _ = self.classify(parts('new_name'), original)
        angle = 0.71
        rotation = np.array([[math.cos(angle), 0, math.sin(angle)], [0, 1, 0],
                             [-math.sin(angle), 0, math.cos(angle)]])
        transformed = {}
        for key, f in original.items():
            triangles = (f['triangles']@rotation.T)*1000+[8000, 10000, 15000]
            points = triangles.reshape((-1, 3))
            # Deduplicate triangle vertices to retain true boundary-edge topology.
            unique, inverse = np.unique(points, axis=0, return_inverse=True)
            cells = np.column_stack((np.full(len(triangles), 3), inverse.reshape((-1, 3)))).ravel()
            transformed[key] = face(unique, cells, f['adjacent_faces'])
        rotated, _, _ = self.classify(parts('new_name'), transformed)
        self.assertEqual({k: v['type'] for k, v in assigned.items()}, {k: v['type'] for k, v in rotated.items()})

    def test_supplied_volume_names_are_optional_region_hints(self):
        bodies = parts('generic1', 'generic2', 'generic3')
        names = ['ns_stator_1_flowdomain', 'ns_impeller_1_flowdomainvirtual', 'ns_volute_1_flowdomainvirtual']
        for body, name in zip(bodies.values(), names):
            body['volume_zones'] = [name]
        faces = {(i, i): square(z=i*10) for i in bodies}
        _, regions, _ = self.classify(bodies, faces)
        self.assertEqual([regions[i]['role'] for i in bodies], ['stationary', 'impeller', 'volute'])

    def test_changed_region_names_use_geometry_and_contacts(self):
        def cloud(dimensions):
            return np.array([[x, y, z] for x in (-dimensions[0]/2, dimensions[0]/2)
                             for y in (-dimensions[1]/2, dimensions[1]/2)
                             for z in (-dimensions[2]/2, dimensions[2]/2)])
        faces = {(1, 1): {'points': cloud((4, 4, 1))},
                 (2, 2): {'points': cloud((8, 8, 3))},
                 (3, 3): {'points': cloud((0.5, 0.5, 6))}}
        bodies = parts('body_A', 'body_B', 'body_C')
        regions, _ = m.infer_regions(bodies, faces, [((1, 1), (2, 2)), ((1, 1), (3, 3))])
        self.assertEqual([regions[i]['role'] for i in bodies], ['impeller', 'volute', 'stationary'])
        self.assertTrue(all(r['uncertain'] for r in regions.values()))

    def test_only_remaining_walls_receive_prism_label_despite_old_label(self):
        class Part:
            id = 1
            name = 'anything'
            labels = {'__pump_wall': {1, 2, 3, 4}, 'label0:xxx': {1, 2, 3, 4}}
            zones = {}
            def get_topo_faces(self):
                return [1, 2, 3, 4]
            def get_labels(self):
                return list(self.labels)
            def remove_zone_on_topo_entities(self, faces):
                for fs in self.zones.values():
                    fs.difference_update(faces)
            def add_topo_entities_to_zone(self, zone, faces):
                self.zones.setdefault(zone, set()).update(faces)
            def add_labels_on_topo_entities(self, labels, faces):
                for label in labels:
                    self.labels.setdefault(label, set()).update(faces)
            def remove_labels_from_topo_entities(self, labels, faces):
                for label in labels:
                    self.labels.setdefault(label, set()).difference_update(faces)
            def get_topo_faces_of_zone_name_pattern(self, name, params):
                return self.zones.get(name, set())
            def get_face_zones(self):
                return list(self.zones)
            def get_volume_zones(self):
                return []
        part = Part()
        model = SimpleNamespace(parts=[part], get_zone_name=lambda zone: zone,
            create_zone=lambda name, kind: SimpleNamespace(zone_id={
                'auto_inlet': 'auto_outlet', 'auto_outlet': 'auto_outlet_1'}.get(name, name)))
        names = ('auto_inlet', 'auto_outlet', 'auto_interface_1', 'auto_stationary_wall')
        cache = {(1, name): {i} for i, name in enumerate(names, 1)}
        api = SimpleNamespace(ZoneType=SimpleNamespace(FACE=1), NamePatternParams=lambda model: None)
        with patch.object(m, 'prime', api), patch.object(m, 'BOUNDARIES', names), \
             patch.object(m, 'WALLS', ('auto_stationary_wall',)), patch.object(m, 'AUTO_FACE_SCOPES', cache):
            m.prepare_boundaries(model)
            self.assertEqual(m.AUTO_FACE_SCOPES[(1, 'auto_outlet')], {1})
            self.assertEqual(m.AUTO_FACE_SCOPES[(1, 'auto_outlet_1')], {2})
            self.assertEqual(part.labels['__pump_wall'], {4})
            self.assertEqual(part.labels['__pump_all'], {1, 2, 3, 4})
            self.assertEqual(m.verify_auto_boundaries(model)[0]['part'], 'anything')

    def test_explicit_solid_removed_and_mapping_keeps_generic_label(self):
        fluid = SimpleNamespace(id=1, name='anything', get_labels=lambda: ['label0:xxx'],
                                get_face_zones=lambda: [], get_volume_zones=lambda: [10])
        solid = SimpleNamespace(id=2, name='casing', get_labels=lambda: [],
                                get_face_zones=lambda: [], get_volume_zones=lambda: [20])
        model = SimpleNamespace(parts=[fluid, solid], get_zone_name=lambda z: {10: 'unknown', 20: 'solid'}[z])
        def delete(ids):
            model.parts = [p for p in model.parts if p.id not in ids]
        model.delete_parts = delete
        faces = {(1, 1): square()}
        with tempfile.TemporaryDirectory() as temp, patch.object(m, 'read_cad_faces', return_value=faces):
            old, roles = m.inspect_names(model, Path(temp))
            self.assertEqual([p.id for p in model.parts], [1])
            self.assertEqual(set(roles), {1})
            self.assertEqual(len(old), 2)
            with (Path(temp)/'auto_boundary_mapping.csv').open(encoding='utf-8-sig', newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['original_label_or_id'], 'label0:generic')
            self.assertEqual(rows[0]['zone_type'], 'wall')
            self.assertEqual(rows[0]['uncertain'], 'True')


if __name__ == '__main__':
    unittest.main()
