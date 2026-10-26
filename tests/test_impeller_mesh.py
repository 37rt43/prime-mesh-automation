"""Offline checks for unit invariance and exact named-selection preservation."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from contextlib import redirect_stdout
from io import StringIO
import impeller_mesh as mesh


class ImpellerTests(unittest.TestCase):
    def test_units_scale_all_lengths(self):
        a = mesh.estimate_sizing((100, 80, 30), [10, 15])
        b = mesh.estimate_sizing((.1, .08, .03), [.01, .015])
        for name in a.__dataclass_fields__:
            self.assertAlmostEqual(getattr(a, name) * .001, getattr(b, name))
        self.assertLess(a.minimum, a.blade)
        self.assertLess(a.blade, a.base)
        total = a.first_height * (mesh.GROWTH**mesh.LAYERS-1)/(mesh.GROWTH-1)
        self.assertLessEqual(total, 10*.015)

    def test_adaptive_coarsening_can_relax_feature_caps(self):
        a = mesh.estimate_sizing((100, 80, 30), [.01])
        b = mesh.estimate_sizing((100, 80, 30), [.01], scale=2)
        self.assertAlmostEqual(b.blade, 2*a.blade)
        self.assertAlmostEqual(b.first_height, 2*a.first_height)

    def test_degenerate_or_invalid_domain_rejected(self):
        for dims in [(1, 2, 0), (1, -1, 2), (1, 2, float('nan'))]:
            with self.assertRaises(ValueError):
                mesh.estimate_sizing(dims, [])

    def boundary_model(self, labels, zones=None):
        zones = zones or {}
        part = Mock(id=1)
        part.name = 'fluid'
        part.get_topo_faces.return_value = [1, 2, 3, 4, 5]
        part.get_labels.side_effect = lambda: list(labels)
        part.get_face_zones.return_value = list(zones)
        part.get_volume_zones.return_value = []
        part.get_topo_faces_of_label_name_pattern.side_effect = lambda name, _: labels[name]
        part.get_topo_faces_of_zone_name_pattern.side_effect = lambda name, _: [
            f for z, fs in zones.values() if z == name for f in fs]
        part.add_topo_entities_to_zone.return_value = None
        part.add_labels_on_topo_entities.return_value = None
        part.add_zonelets_to_zone.return_value = None
        part.add_labels_on_zonelets.return_value = None
        model = Mock(parts=[part])
        model.get_zone_name.side_effect = lambda z: zones[z][0]
        model.create_zone.return_value = SimpleNamespace(zone_id=99)
        return model, part

    @patch.object(mesh.prime, 'NamePatternParams', return_value=None)
    def test_missing_names_warn_and_continue_without_topology_check(self, *_):
        model, part = self.boundary_model({'inlet': [1], 'INLET': [2], 'inlet-extra': [3]})
        output = StringIO()
        with redirect_stdout(output):
            groups = mesh.load_named_zones(model)
        self.assertEqual(groups['inlet'][1]['faces'], [1])
        self.assertIn("'interface-1'", output.getvalue())
        part.get_topo_volumes.assert_not_called()
        queried = [call.args[0] for call in part.get_topo_faces_of_label_name_pattern.call_args_list]
        self.assertEqual(queried, ['inlet'])

    @patch.object(mesh.prime, 'NamePatternParams', return_value=None)
    def test_volute_label_without_faces_is_recognized_and_continues(self, *_):
        model, part = self.boundary_model({'volute': []})
        output = StringIO()
        with redirect_stdout(output):
            groups = mesh.load_named_zones(model)
            mesh.prepare_named_zones(model, groups)
        self.assertTrue(groups['volute'][1]['recognized'])
        self.assertEqual(groups['volute'][1]['faces'], [])
        self.assertNotIn("未找到命名选择/zone 'volute'", output.getvalue())
        self.assertIn("已识别 'volute'", output.getvalue())
        self.assertIn("未找到命名选择/zone 'blade'", output.getvalue())
        model.create_zone.assert_not_called()
        part.get_topo_volumes.assert_not_called()

    @patch.object(mesh.prime, 'NamePatternParams', return_value=None)
    def test_volute_body_group_without_faces_is_recognized(self, *_):
        model, part = self.boundary_model({'interface-3': [], 'volute': []})
        part.name = 'volute'
        part.get_topo_faces.return_value = []
        part.get_face_zonelets.return_value = []
        part.get_face_zonelets_of_label_name_pattern.return_value = []
        output = StringIO()
        with redirect_stdout(output):
            groups = mesh.load_named_zones(model)
            mesh.prepare_named_zones(model, groups)
        self.assertTrue(groups['volute'][1]['recognized'])
        self.assertTrue(groups['volute'][1]['region'])
        self.assertTrue(groups['interface-3'][1]['recognized'])
        self.assertNotIn("未找到命名选择/zone 'volute'", output.getvalue())
        self.assertNotIn("未找到命名选择/zone 'interface-3'", output.getvalue())

    @patch.object(mesh.prime, 'NamePatternParams', return_value=None)
    def test_interfaces_stay_separate_and_out_of_prism_walls(self, *_):
        model, part = self.boundary_model({
            'impeller': [1, 2, 3, 4, 5], 'blade': [5], 'inlet': [4]},
            {11: ('interface-1', [1]), 12: ('interface-2', [2]), 13: ('interface-3', [3])})
        with redirect_stdout(StringIO()):
            groups = mesh.load_named_zones(model)
        mesh.prepare_named_zones(model, groups)
        for name, face in zip(mesh.INTERFACES, [1,2,3]):
            self.assertEqual(groups[name][1]['faces'], [face])
        wall_calls = [c.args[1] for c in part.add_labels_on_topo_entities.call_args_list
                      if c.args[0] == ['mesh_prism_wall']]
        self.assertTrue(wall_calls)
        self.assertTrue(all(set(fs) <= {5} for fs in wall_calls))
        self.assertEqual([c.args[0] for c in part.add_topo_entities_to_zone.call_args_list][-3:],
                         [11,12,13])
        part.merge_zonelets.assert_not_called()

    @patch.object(mesh.prime, 'NamePatternParams', return_value=None)
    def test_named_part_scopes_refinement_without_becoming_face_zone(self, *_):
        model, part = self.boundary_model({})
        part.name = 'Seal_Chamber'
        with redirect_stdout(StringIO()):
            groups = mesh.load_named_zones(model)
        mesh.prepare_named_zones(model, groups)
        self.assertEqual(groups['Seal_Chamber'][1]['faces'], [1,2,3,4,5])
        model.create_zone.assert_not_called()
        part.add_labels_on_topo_entities.assert_any_call(
            ['mesh_refine_Seal_Chamber'], [1,2,3,4,5])


if __name__ == '__main__':
    unittest.main()
