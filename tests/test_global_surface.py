"""Global coarse surfaces, virtual metadata and fresh-import retries; offline mocks."""
from contextlib import nullcontext
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import pump_mesh_auto as m


def constructor(model, **kwargs):
    return NS(**kwargs)


def part(pid, name, faces, volumes, zones, cells=0):
    return NS(id=pid, name=name, get_topo_faces=Mock(return_value=faces),
        get_topo_volumes=Mock(return_value=volumes), get_volume_zones=Mock(return_value=zones),
        get_summary=Mock(return_value=NS(n_faces=10 if faces else 0, n_cells=cells, n_unmeshed_topo_faces=0)))


class GlobalSurfaceTests(unittest.TestCase):
    def setUp(self):
        m.invalidate_queries()

    def api(self):
        return NS(PartSummaryParams=constructor, MergePartsParams=constructor,
            SurferParams=constructor, GlobalSizingParams=constructor,
            SizeFieldType=NS(CONSTANT='constant'), AutoMeshParams=constructor,
            VolumeControlParams=constructor, ScopeDefinition=constructor,
            CellZoneletType=NS(FLUID='fluid'), ScopeEntity=NS(VOLUME='volume', FACEZONELETS='faces'),
            ScopeEvaluationType=NS(ZONES='zones'), VolumeFillType=NS(TET='tet'),
            SurfaceDiagnosticSummaryParams=constructor)

    def model(self):
        a = part(1, 'ns_stator_1_flowdomain', [11, 12], [111], [10])
        b = part(2, 'ns_impeller_1_flowdomainvirtual', [13], [112], [20])
        empty = part(3, 'ns_volute_1_flowdomainvirtual', [], [], [30])
        owner = part(99, 'auto_global_fluid_surface', [11, 12, 13], [111, 112], [10, 20], cells=20)
        model = NS(parts=[a, b, empty],
            get_zone_name=Mock(side_effect=lambda z: {10: 'stator', 20: 'impeller_virtual', 30: 'volute_virtual'}[z]),
            get_part=Mock(return_value=owner), set_global_sizing_params=Mock(return_value=None))
        def merge(ids, params):
            model.parts = [owner, empty]
            return NS(merged_part_id=99)
        model.merge_parts = Mock(side_effect=merge)
        vc = NS(id=77, set_params=Mock(), set_scope=Mock(return_value=None))
        model.control_data = NS(create_volume_control=Mock(return_value=vc))
        return model, owner

    def test_single_surface_union_not_individual_virtual_parts(self):
        model, owner = self.model()
        api = self.api()
        surface = NS(mesh_topo_faces=Mock(return_value=None))
        volume = NS(mesh=Mock(return_value=None))
        api.Surfer, api.AutoMesh = Mock(return_value=surface), Mock(return_value=volume)
        scopes = {(1, 'auto_inlet'): {11}, (1, 'auto_stationary_wall'): {12}, (2, 'auto_impeller_wall'): {13}}
        mapping = [{'original_part_id': 1}, {'original_part_id': 2}]
        with patch.object(m, 'prime', api), patch.object(m, 'AUTO_FACE_SCOPES', scopes), \
             patch.object(m, 'AUTO_MAPPING', mapping), patch.object(m, 'MESH_PART_IDS', None):
            diagnostics = {}
            fills = m.mesh_coarse(model, {'base': 0.1}, m.DEFAULTS, {}, diagnostics=diagnostics)
            self.assertEqual(fills, {99: 'TET'})
            model.merge_parts.assert_called_once()
            self.assertEqual(model.merge_parts.call_args.args[0], [1, 2])
            surface.mesh_topo_faces.assert_called_once()
            self.assertEqual(surface.mesh_topo_faces.call_args.args[:2], (99, [11, 12, 13]))
            self.assertEqual(volume.mesh.call_args.args[0], 99)
            self.assertEqual(diagnostics['metadata_only_part_ids'], [3])
            self.assertEqual(m.AUTO_FACE_SCOPES[(99, 'auto_impeller_wall')], {13})
            self.assertEqual(mapping[1]['mesh_part_id'], 99)
            self.assertEqual(m.MESH_PART_IDS, {99})
            self.assertNotIn('merge_volumes', model.__dict__)
            self.assertEqual(diagnostics['source_parts'][2]['topo_face_count'], 0)
            self.assertEqual(diagnostics['source_parts'][2]['surface_action'], 'skip_individual_surface_call_no_topo_faces')

    def test_empty_global_owner_has_no_surfer_call(self):
        api = self.api()
        api.Surfer = Mock(side_effect=AssertionError('must not mesh empty topology'))
        owner = part(5, 'virtual_only', [], [], [])
        with patch.object(m, 'prime', api):
            self.assertFalse(m.mesh_global_surface(NS(), owner, 0.1, 'constant', {}))
        api.Surfer.assert_not_called()

    def test_geometry_measurement_skips_empty_virtual_container(self):
        valid = part(1, 'stator', [11, 12], [111], [10])
        empty = part(2, 'impeller_virtual', [], [], [20])
        topo = NS(get_geom_zonelets_of_topo_faces=Mock(return_value=[50]))
        model = NS(parts=[empty, valid], topo_data=topo)
        api = self.api()
        box = NS(xmin=0, ymin=0, zmin=0, xmax=1, ymax=2, zmax=3)
        api.SurfaceUtilities = Mock(return_value=NS(get_bounding_box_of_zonelets=Mock(return_value=box)))
        with patch.object(m, 'prime', api):
            geometry = m.measure(model, coarse=True)
        self.assertEqual(set(geometry['parts']), {1})
        self.assertEqual(geometry['overall']['extents'], [1, 2, 3])
        topo.get_geom_zonelets_of_topo_faces.assert_called_once_with([11, 12])

    def test_no_global_geometry_reports_real_missing_geometry(self):
        model = NS(parts=[part(3, 'virtual_only', [], [], [])])
        with patch.object(m, 'prime', self.api()), self.assertRaisesRegex(RuntimeError, 'No valid topo faces anywhere'):
            m.global_surface_owner(model, {})

    def test_failed_merge_cannot_silently_drop_fluid_faces(self):
        model, owner = self.model()
        owner.get_topo_faces.return_value = [11, 12]
        with patch.object(m, 'prime', self.api()), self.assertRaisesRegex(RuntimeError, 'changed face/volume topology IDs'):
            m.global_surface_owner(model, {})

    def test_surface_failure_reports_owner_source_counts_and_boundary_edges(self):
        model, owner = self.model()
        api = self.api()
        api.Surfer = Mock(return_value=NS(mesh_topo_faces=Mock(side_effect=RuntimeError('Surface meshing failed'))))
        api.SurfaceSearch = Mock(return_value=NS(get_surface_diagnostic_summary=Mock(return_value=NS(n_free_edges=7))))
        model.topo_data = NS(get_topo_edges_of_topo_faces=Mock(return_value=[1, 2, 3]))
        diagnostics = {'source_parts': [{'part_id': 2, 'part_name': 'impeller_virtual', 'topo_face_count': 1}]}
        with patch.object(m, 'prime', api), self.assertLogs(m.LOG, 'ERROR'), self.assertRaises(m.SurfaceMeshingFailure) as caught:
            m.mesh_global_surface(model, owner, 0.1, 'constant', diagnostics)
        row = caught.exception.diagnostics
        self.assertEqual((row['part_id'], row['part_name'], row['topo_face_count']), (99, owner.name, 3))
        self.assertEqual(row['face_count'], 10)
        self.assertEqual(row['boundary_edge_count'], 7)
        self.assertEqual(row['topo_edge_count'], 3)
        self.assertIsNone(row['topo_boundary_edge_count'])
        model.topo_data.get_topo_edges_of_topo_faces.assert_called_once()

    def test_surfer_defaults_are_an_explicit_retry_option(self):
        api = self.api()
        api.SurferParams = Mock(side_effect=constructor)
        api.Surfer = Mock(return_value=NS(mesh_topo_faces=Mock(return_value=None)))
        owner = part(99, 'global', [11, 12], [111], [10])
        with patch.object(m, 'prime', api):
            m.mesh_global_surface(NS(), owner, 0.1, 'default', {})
        self.assertEqual(api.SurferParams.call_args.kwargs, {})
        self.assertEqual([m.surface_retry_plan(n) for n in range(3)], ['constant', 'constant', 'default'])

    def test_surface_retries_reimport_fresh_models_and_reduce_size(self):
        cad_models, sizes, strategies = [], [], []
        def launch():
            model = NS(parts=[])
            cad_models.append(model)
            return nullcontext(NS(model=model))
        def mesh(model, size, cfg, members, **kwargs):
            sizes.append(size['base'])
            strategies.append(kwargs['surface_strategy'])
            if len(sizes) < 3:
                raise m.SurfaceMeshingFailure('synthetic offline failure', {'part_id': 99})
            return {99: 'TET'}
        with tempfile.TemporaryDirectory() as temp:
            cad = Path(temp)/'dummy.scdoc'
            cad.write_text('test fixture, never imported')
            output = Path(temp)/'out'
            with patch.object(m, 'launch_prime', side_effect=launch), \
                 patch.object(m, 'observe_prime_calls', return_value=nullcontext()), \
                 patch.object(m, 'import_geometry') as importer, \
                 patch.object(m, 'audit_imported_geometry', return_value=[]), \
                 patch.object(m, 'inspect_names', return_value=([], {})), \
                 patch.object(m, 'measure', return_value={'overall': {'extents': [3, 4, 12]}}), \
                 patch.object(m, 'prepare_boundaries'), patch.object(m, 'write_mapping'), \
                 patch.object(m, 'mesh_coarse', side_effect=mesh), \
                 patch.object(m, 'quality', return_value=([], set())), \
                 patch.object(m, 'verify_auto_boundaries', return_value=[]), \
                 patch.object(m, 'export_fluent_mesh', return_value=output/'pump.msh'), \
                 patch.object(m, 'AUTO_RECOGNITION', {}), patch.object(m, 'AUTO_MAPPING', []), \
                 patch.object(m.logging, 'FileHandler', return_value=logging.NullHandler()), \
                 patch.object(m.logging, 'basicConfig'), self.assertLogs(m.LOG, 'ERROR'):
                result = m.run(['--coarse', '--cad', str(cad), '--output', str(output), '--max-iterations', '0'])
            self.assertEqual(result, 0)
            self.assertEqual(importer.call_count, 3)
            self.assertEqual(len({id(model) for model in cad_models}), 3)
            self.assertEqual(strategies, ['constant', 'constant', 'default'])
            self.assertGreater(sizes[0], sizes[1])
            self.assertGreater(sizes[1], sizes[2])
            report = json.loads((output/'mesh_quality_report.json').read_text())
            self.assertEqual(len(report['attempts']), 3)
            self.assertTrue(report['attempts'][0]['surface_retry']['fresh_CAD_import'])


if __name__ == '__main__':
    unittest.main()
