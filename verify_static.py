"""Static/API checks only. Never calls Prime launch/model/CAD/mesh operations."""
import ast
import importlib.util
import inspect
from pathlib import Path
import sys
import ansys.meshing.prime as prime

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location('pump_mesh_auto', root/'pump_mesh_auto.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
source = (root/'pump_mesh_auto.py').read_text()
tree = ast.parse(source, feature_version=(3,11))
count = 0
for n in ast.walk(tree):
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == 'prime':
        obj = getattr(prime, n.func.attr)
        sig = inspect.signature(obj)
        # Generated types accept **kwargs but ignore invalid names: reject them explicitly.
        allowed = set(sig.parameters)
        invalid = {k.arg for k in n.keywords if k.arg is not None} - allowed
        assert not invalid, (n.lineno, n.func.attr, invalid)
        count += 1
# Verify every method used with a Prime object against its real class signature.
methods = {
 'Model': ['create_zone', 'get_zone_name', 'set_global_sizing_params', 'delete_volumetric_size_fields'],
 'Part': ['get_labels','get_topo_faces','get_topo_edges','get_topo_volumes','get_face_zones',
          'get_volume_zones','get_topo_faces_of_label_name_pattern','get_topo_faces_of_zone_name_pattern',
          'add_topo_entities_to_zone','add_labels_on_topo_entities','compute_topo_volumes',
          'get_summary','get_cell_zonelets'],
 'TopoData': ['get_geom_zonelets_of_topo_faces','get_geom_zonelets_of_topo_edges'],
 'ControlData': ['create_size_control','create_prism_control','create_volume_control'],
 'SizeControl': ['set_scope','set_curvature_sizing_params','set_proximity_sizing_params','set_soft_sizing_params'],
 'PrismControl': ['set_surface_scope','set_volume_scope','set_growth_params'],
 'VolumeControl': ['set_scope','set_params'],
 'FileIO': ['import_cad','export_fluent_meshing_mesh'],
 'SurfaceUtilities': ['get_bounding_box_of_zonelets'],
 'SizeField': ['compute_volumetric'], 'AutoMesh': ['mesh'],
 'VolumeSearch': ['get_volume_quality_summary'],
 'SurfaceSearch': ['get_surface_quality_summary'],
 'VolumeMeshTool': ['improve_by_auto_node_move'],
}
for cls, names in methods.items():
    for name in names:
        assert callable(getattr(getattr(prime, cls), name)), (cls,name)
        inspect.signature(getattr(getattr(prime, cls),name))
assert callable(prime.lucid.Mesh.surface_mesh_with_size_controls)
# Import and CLI parser checks, with a launch trap to prove these checks are offline.
def forbidden():
    raise AssertionError('No launch permitted in static verification')
m.launch_prime = forbidden
assert m.arguments([]).cad.name == 'FFF-8.scdoc'
assert m.arguments(['--dry-run']).dry_run
cfg = m.configuration(None)
def geom(scale):
    return {'overall': {'extents':[10*scale,20*scale,30*scale]},
            'parts': {1:{'extents':[10*scale,20*scale,30*scale],
                         'edge_bbox_diagonals':[scale,2*scale,3*scale]}}}
a = m.sizing(geom(1),cfg,0,set())
b = m.sizing(geom(1000),cfg,0,set())
for name in ('local','minimum','base','first_height_requested','total_thickness_requested'):
    assert abs(b['parts'][1][name]/a['parts'][1][name] - 1000)<1e-8, name
assert a['parts'][1]['layer_count_requested'] == b['parts'][1]['layer_count_requested']
assert m.sizing(geom(1),cfg,1,{1})['parts'][1]['local'] < a['parts'][1]['local']
for fill in ('POLY','HEXCOREPOLY','TET'):
    assert hasattr(prime.VolumeFillType,fill)
print('Python syntax parsed with Python 3.11 grammar: PASS')
print('Import main workflow without launching Prime: PASS')
print(f'PyPrimeMesh {prime.__version__}: {count} constructor/call keyword checks: PASS')
print(f'{sum(map(len,methods.values()))} Prime method existence/signature checks: PASS')
print('CLI defaults, scale invariance (1:1000), repair sizing reduction: PASS')
print('Prime server starts / CAD imports / meshing calls executed: ZERO')
print('Host check interpreter: '+sys.version.split()[0]+' (not local 3.11 runtime validation)')
