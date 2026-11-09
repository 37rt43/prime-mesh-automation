"""Coarse workflow and heartbeat tests: mocks only, never a Prime process."""
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import pump_mesh_auto as m


class CoarseTests(unittest.TestCase):
    def setUp(self):
        m.invalidate_queries()

    def test_coarse_cli_and_scale_invariance(self):
        self.assertTrue(m.arguments(['--coarse']).coarse)
        self.assertEqual(m.arguments([]).prime_timeout, 900)
        a = m.coarse_sizing({'overall': {'extents': [3, 4, 12]}}, m.DEFAULTS, 0)
        b = m.coarse_sizing({'overall': {'extents': [3000, 4000, 12000]}}, m.DEFAULTS, 0)
        self.assertAlmostEqual(b['base']/a['base'], 1000)
        self.assertEqual(a['global_control_count'], 1)
        self.assertEqual(a['local_controls'], 0)
        self.assertEqual(a['prism_layers'], 0)
        self.assertLess(m.coarse_sizing({'overall': {'extents': [3, 4, 12]}}, m.DEFAULTS, 1)['base'], a['base'])

    def test_constant_coarse_path_has_no_sizing_objects_fields_or_prisms(self):
        part = Mock()
        part.id, part.name = 1, 'body'
        part.get_topo_faces.return_value = [11, 12]
        part.get_topo_volumes.return_value = [22]
        part.get_volume_zones.return_value = [33]
        part.get_summary.return_value = NS(n_faces=20, n_cells=30, n_unmeshed_topo_faces=0)
        part.get_labels.side_effect = AssertionError('coarse mesh must not query labels')
        part.get_face_zones.side_effect = AssertionError('coarse mesh must not query face zones')
        model = Mock(parts=[part])
        model.set_global_sizing_params.return_value = None
        vc = Mock(id=44)
        vc.set_scope.return_value = None
        model.control_data.create_volume_control.return_value = vc
        model.control_data.create_size_control.side_effect = AssertionError('no local/global SizeControl objects')
        model.control_data.create_prism_control.side_effect = AssertionError('no prisms')
        model.control_data.create_size_control.assert_not_called()
        surface = Mock()
        surface.mesh_topo_faces.return_value = None
        volume = Mock()
        volume.mesh.return_value = None
        constructor = lambda model, **kwargs: NS(**kwargs)
        api = NS(GlobalSizingParams=constructor, SurferParams=constructor,
            AutoMeshParams=constructor, PartSummaryParams=constructor,
            VolumeControlParams=constructor, ScopeDefinition=constructor,
            SizeFieldType=NS(CONSTANT='constant'), VolumeFillType=NS(TET='tet'),
            CellZoneletType=NS(FLUID='fluid'), ScopeEntity=NS(VOLUME='volume'),
            ScopeEvaluationType=NS(ZONES='zones'), Surfer=Mock(return_value=surface),
            AutoMesh=Mock(return_value=volume))
        walls = tuple('auto_wall_'+str(i) for i in range(65))
        with patch.object(m, 'prime', api), patch.object(m, 'BOUNDARIES', walls):
            fills = m.mesh_coarse(model, {'base': 0.3}, m.DEFAULTS, {1: 'volute'})
        self.assertEqual(fills, {1: 'TET'})
        model.set_global_sizing_params.assert_called_once()
        model.control_data.create_size_control.assert_not_called()
        model.control_data.create_prism_control.assert_not_called()
        model.delete_volumetric_size_fields.assert_not_called()
        surface.mesh_topo_faces.assert_called_once()
        volume.mesh.assert_called_once()
        params = volume.mesh.call_args.args[1]
        self.assertEqual(params.prism_control_ids, [])
        self.assertEqual(params.size_field_type, 'constant')
        self.assertEqual(params.volume_fill_type, 'tet')
        part.get_labels.assert_not_called()
        part.get_face_zones.assert_not_called()

    def test_known_missing_auto_scopes_do_not_make_65_remote_queries(self):
        part = Mock(id=1)
        names = tuple('auto_wall_'+str(i) for i in range(65))
        with patch.object(m, 'BOUNDARIES', names), patch.object(m, 'AUTO_FACE_SCOPES', {}):
            for name in names:
                self.assertEqual(m.face_ids(Mock(), part, name), set())
        part.get_labels.assert_not_called()
        part.get_face_zones.assert_not_called()

    def test_query_cache_and_explicit_invalidation(self):
        part = NS(get_labels=Mock(side_effect=[['old'], ['new']]))
        self.assertEqual(m.cached_query(part, 'get_labels'), ['old'])
        self.assertEqual(m.cached_query(part, 'get_labels'), ['old'])
        part.get_labels.assert_called_once()
        m.invalidate_queries()
        self.assertEqual(m.cached_query(part, 'get_labels'), ['new'])

    def test_heartbeat_timeout_diagnostic_is_once_without_cancelling_rpc(self):
        with tempfile.TemporaryDirectory() as temp:
            monitor = m.RuntimeMonitor(Path(temp), timeout=1)
            monitor.stages.append(('coarse surface_mesh body', time.monotonic()-5))
            monitor.calls.append(('serve: surface_mesh', time.monotonic()-3))
            with patch.object(m.faulthandler, 'dump_traceback') as dump, self.assertLogs(m.LOG, 'INFO') as logs:
                monitor.pulse()
                monitor.pulse()
            dump.assert_called_once()
            self.assertTrue(any('HEARTBEAT' in line for line in logs.output))
            self.assertTrue(any('TIMEOUT DIAGNOSTIC' in line for line in logs.output))
            state = json.loads((Path(temp)/'prime_runtime_status.json').read_text())
            self.assertEqual(state['prime_call'], 'serve: surface_mesh')
            self.assertTrue(state['timeout_is_diagnostic_only'])

    def test_rpc_observer_restores_communicator_after_failure(self):
        comm = NS(serve=Mock(side_effect=RuntimeError('failed')), initialize_params=Mock())
        original = comm.serve
        with tempfile.TemporaryDirectory() as temp, patch.object(m, 'MONITOR', m.RuntimeMonitor(Path(temp), 900)):
            with self.assertRaises(RuntimeError):
                with m.observe_prime_calls(NS(_communicator=comm)):
                    comm.serve(None, 'PrimeMesh::Surfer/MeshTopoFaces')
            self.assertIs(comm.serve, original)
            self.assertEqual(m.MONITOR.calls, [])

    def test_recognition_budget_is_bounded_and_reports_wall_fallback(self):
        import numpy as np
        f = m.facet_descriptor(np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]]), [3, 0, 1, 2])
        f.update(original_labels=['label0:xxx'], adjacent_faces=[])
        with self.assertRaises(m.RecognitionBudgetExceeded):
            m.classify_boundary_faces({1: {}}, {(1, 1): f}, m.DEFAULTS, deadline=time.monotonic()-1)
        part = NS(id=1, name='renamed', get_labels=lambda: [], get_face_zones=lambda: [], get_volume_zones=lambda: [])
        model = NS(parts=[part])
        with tempfile.TemporaryDirectory() as temp, patch.object(m, 'read_cad_faces', return_value={(1, 1): f}), \
             patch.object(m, 'classify_boundary_faces', side_effect=m.RecognitionBudgetExceeded):
            m.inspect_names(model, Path(temp))
            self.assertEqual(m.AUTO_MAPPING[0]['zone_type'], 'wall')
            self.assertTrue(m.AUTO_MAPPING[0]['uncertain'])
            self.assertTrue(m.AUTO_RECOGNITION['details']['recognition_budget_exceeded'])
            self.assertTrue((Path(temp)/'auto_boundary_mapping.csv').exists())


if __name__ == '__main__':
    unittest.main()
