"""Offline checks for unit invariance and unsafe boundary classification."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
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

    def boundary_model(self, labels):
        part = SimpleNamespace(id=1, name='fluid', get_topo_faces=lambda: [1,2,3,4,5],
            get_topo_volumes=lambda: [10], get_labels=lambda: list(labels),
            get_topo_faces_of_label_name_pattern=lambda label, params: labels[label])
        return SimpleNamespace(parts=[part])

    @patch.object(mesh.prime, 'NamePatternParams', return_value=None)
    @patch.object(mesh, 'face_entities', return_value=([1,2,3,4,5], []))
    def test_boundaries_preserve_unmatched_wall(self, *_):
        model = self.boundary_model({'入口':[1], '出口':[2], '叶片':[3], '蜗壳':[4]})
        groups = mesh.identify_boundaries(model, mesh.DEFAULT_PATTERNS)
        self.assertEqual(groups['inlet'][1], [1])
        self.assertEqual(groups['wall_other'][1], [5])

    @patch.object(mesh.prime, 'NamePatternParams', return_value=None)
    @patch.object(mesh, 'face_entities', return_value=([1,2,3,4,5], []))
    def test_ambiguous_or_missing_semantics_rejected(self, *_):
        for labels in [{'inlet':[1]}, {'inlet':[1], 'outlet':[1], 'blade':[3], 'volute':[4]}]:
            with self.assertRaises(RuntimeError):
                mesh.identify_boundaries(self.boundary_model(labels), mesh.DEFAULT_PATTERNS)


if __name__ == '__main__':
    unittest.main()
