from __future__ import print_function, absolute_import

from functools import reduce
import enum
import math
import operator
import sys
import uuid

import numpy as np

from IPython.display import display

import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from pythreejs import BufferAttribute, BufferGeometry, CombinedCamera, GridHelper, PointLight,\
                          AmbientLight, Scene, OrbitControls, Renderer, Mesh, MeshLambertMaterial,\
                          LineSegmentsGeometry, LineMaterial, LineSegments2, MeshStandardMaterial

from OCC.Core.Visualization import Tesselator
from OCC.Extend.TopologyUtils import TopologyExplorer, is_edge, discretize_edge

from cadquery import Compound, Vector

from .tree_view import state_diff

HAVE_SMESH = False
SERVER_SIDE = 1
CLIENT_SIDE = 2

def _decomp(a):
    [item] = a.items()
    return item

def explode(edge_list):
    return [[edge_list[i], edge_list[i + 1]] for i in range(len(edge_list) - 1)]

def flatten(nested_list):
    return [y for x in nested_list for y in x]

class CadqueryView(object):

    def __init__(self, width=600, height=400, render_edges=True, debug=None):
        self._compute_normals_mode = SERVER_SIDE
        self.features = ["mesh", "edges"]
        self.default_shape_color = self._format_color(166, 166, 166)
        self.default_mesh_color = 'white'
        self.default_edge_color = self._format_color(0, 0, 0)
        self.default_selection_material = self._material('orange')
        self._debug = debug

        self.width = width
        self.height = height
        self.render_edges = render_edges

        self.shapes = []
        self.rendered_shapes = []

        self.camera = None
        self.axes = None
        self.grid = None
        self.scene = None
        self.controller = None
        self.renderer = None

    def _format_color(self, r, g, b):
        return '#%02x%02x%02x' % (r, g, b)

    def _material(self, color, transparent=False, opacity=1.0):
        return MeshStandardMaterial(color=color, transparent=transparent, opacity=opacity)

    def _renderShape(self,
        shape=None,  # the TopoDS_Shape to be displayed
        edges=None,  # or the edges to be displayed 
        shape_color=None,
        render_edges=False,
        edge_color=None,
        edge_width=1,
        deflection=0.05,
        compute_uv_coords=False,
        quality=1.0,
        transparent=True,
        opacity=0.6):

        edge_list = None
        edge_lines = None

        if shape is not None:
            if shape_color is None:
                shape_color = self.default_shape_color
            if edge_color is None:
                edge_color = self.default_edge_color

            # first, compute the tesselation
            tess = Tesselator(shape)
            tess.Compute(uv_coords=compute_uv_coords, compute_edges=render_edges,
                         mesh_quality=quality, parallel=True)

            # get vertices and normals
            vertices_position = tess.GetVerticesPositionAsTuple()

            number_of_triangles = tess.ObjGetTriangleCount()
            number_of_vertices = len(vertices_position)

            # number of vertices should be a multiple of 3
            if number_of_vertices % 3 != 0:
                raise AssertionError("Wrong number of vertices")
            if number_of_triangles * 9 != number_of_vertices:
                raise AssertionError("Wrong number of triangles")

            # then we build the vertex and faces collections as numpy ndarrays
            np_vertices = np.array(vertices_position, dtype='float32')\
                            .reshape(int(number_of_vertices / 3), 3)
            # Note: np_faces is just [0, 1, 2, 3, 4, 5, ...], thus arange is used
            np_faces = np.arange(np_vertices.shape[0], dtype='uint32')

            # set geometry properties
            buffer_geometry_properties = {
                'position': BufferAttribute(np_vertices),
                'index': BufferAttribute(np_faces)
            }
            if self._compute_normals_mode == SERVER_SIDE:
                # get the normal list, converts to a numpy ndarray. This should not raise
                # any issue, since normals have been computed by the server, and are available
                # as a list of floats
                np_normals = np.array(tess.GetNormalsAsTuple(), dtype='float32').reshape(-1, 3)
                # quick check
                if np_normals.shape != np_vertices.shape:
                    raise AssertionError("Wrong number of normals/shapes")
                buffer_geometry_properties['normal'] = BufferAttribute(np_normals)

            # build a BufferGeometry instance
            shape_geometry = BufferGeometry(attributes=buffer_geometry_properties)

            # if the client has to render normals, add the related js instructions
            if self._compute_normals_mode == CLIENT_SIDE:
                shape_geometry.exec_three_obj_method('computeVertexNormals')

            # then a default material
            shp_material = self._material(shape_color, transparent, opacity)

            # create a mesh unique id
            mesh_id = uuid.uuid4().hex

            # finally create the mash
            shape_mesh = Mesh(geometry=shape_geometry, material=shp_material, name=mesh_id)

            # edge rendering, if set to True

            if render_edges:
                edge_list = list(
                    map(lambda i_edge: [tess.GetEdgeVertex(i_edge, i_vert)
                                        for i_vert in range(tess.ObjEdgeGetVertexCount(i_edge))],
                        range(tess.ObjGetEdgeCount())))

        if edges is not None:
            shape_mesh = None
            edge_list = [discretize_edge(edge, deflection) for edge in edges]

        if edge_list is not None:
            edge_list = flatten(list(map(explode, edge_list)))
            lines = LineSegmentsGeometry(positions=edge_list)
            mat = LineMaterial(linewidth=edge_width, color=edge_color)
            edge_lines = LineSegments2(lines, mat)

        if shape_mesh is not None or edge_lines is not None:
            self.rendered_shapes.append({"mesh": shape_mesh, "edges": edge_lines})

    def _bbox(self, shapes):
        compounds = [Compound.makeCompound(shape.objects) for shape in shapes]
        return Compound.makeCompound(compounds).BoundingBox()

    def _scale(self, vec):
        r = self.bb.DiagonalLength * 1.4
        n = np.linalg.norm(vec)
        new_vec = (vec / n * r).tolist()
        return Vector(new_vec).add(self.bb.center).toTuple()

    def _update(self):
        self.controller.exec_three_obj_method('update')
        pass

    # UI Handler

    def changeView(self, typ, directions):
        def reset(b):
            self.controller.reset()

        def refit(b):
            self.camera.zoom = 1.0
            self._update()

        def change(b):
            self.camera.position = self._scale(directions[typ])
            self._update()

        if typ == "fit":
            return refit
        elif typ == "reset":
            return reset
        else:
            return change

    def setAxes(self, change):
        self.axes.toggleAxes(change)

    def toggleAxes(self, change):
        self.setAxes(change["new"])

    def setCenter(self, change):
        center = (0, 0, 0) if change else self.bb.center.toTuple()
        self.grid.position = center
        for i in range(3):
            self.scene.children[i].position = center

    def toggleCenter(self, change):
        self.setCenter(change["new"])

    def setGrid(self, change):
        self.grid.visible = change

    def toggleGrid(self, change):
        self.setGrid(change["new"])

    def setOrtho(self, change):
        self.camera.mode = 'orthographic' if change else 'perspective'

    def toggleOrtho(self, change):
        self.setOrtho(change["new"])

    def setVisibility(self, ind, i, state):
        feature = self.features[i]
        if self.rendered_shapes[ind][feature] is not None:
            self.rendered_shapes[ind][feature].visible = (state == 1)

    def changeVisibility(self, mapping):
        def f(states):
            diffs = state_diff(states.get("old"), states.get("new"))
            for diff in diffs:
                obj, val = _decomp(diff)
                self.setVisibility(mapping[obj], val["icon"], val["new"])
        return f

    # public methods to add shapes and render the view

    def addShape(self, shape, color="#ff0000"):
        self.shapes.append({"shape": shape, "color": color})

    def render(self):
        for shape in self.shapes:
            if is_edge(shape["shape"].toOCC()):
                # TODO Check it is safe to omit these edges
                # The edges with on1 vertex are CurveOnSurface
                # curve_adaptator = BRepAdaptor_Curve(edge)
                # curve_adaptator.IsCurveOnSurface() == True
                edges = [edge.wrapped
                         for edge in shape["shape"].objects
                         if TopologyExplorer(edge.wrapped).number_of_vertices() >= 2 ]
                self._renderShape(edges=edges,
                                  render_edges=True, edge_color=shape["color"], edge_width=3)
            else:
                self._renderShape(shape=shape["shape"].toOCC(),
                                  render_edges=True, shape_color=shape["color"])

        self.bb = self._bbox([shape["shape"] for shape in self.shapes])
        bb_max = max((abs(self.bb.xmin), abs(self.bb.xmax),
                      abs(self.bb.ymin), abs(self.bb.ymax),
                      abs(self.bb.zmin), abs(self.bb.zmax)))
        camera_target = self.bb.center.toTuple()
        camera_position = self._scale([1, 1, 1])

        self.camera = CombinedCamera(position=camera_position,
                                     width=self.width, height=self.height)
        self.camera.up = (0.0, 0.0, 1.0)
        self.camera.lookAt(camera_target)
        self.camera.mode = 'orthographic'

        self.axes = Axes(length=bb_max)
        self.axes.toggleAxes(True)
        grid_size = math.ceil(self.bb.DiagonalLength)
        self.grid = GridHelper(grid_size, 10, colorCenterLine='#aaa', colorGrid = '#ddd')
        self.grid.position = camera_target

        key_light = PointLight(position=[-100, 100, 100])
        ambient_light = AmbientLight(intensity=0.4)

        children = self.axes.axes + [self.grid, key_light, ambient_light, self.camera]
        for rendered_shape in self.rendered_shapes:
            children = children + [rendered_shape[self.features[0]], rendered_shape[self.features[1]]]

        self.scene = Scene(children=children)

        self.controller = OrbitControls(controlling=self.camera, target=camera_target)

        self.renderer = Renderer(scene=self.scene, camera=self.camera, controls=[self.controller],
                                 width=self.width, height=self.height)

        self.camera.position = self._scale(self.camera.position)

        # needs to be done after setup of camera
        self.grid.rotation = (math.pi / 2.0, 0, 0, "XYZ")
        self.grid.position = (0, 0, 0)

        return self.renderer


class Axes(object):
    def __init__(self, origin=None, length=1, width=3):
        if origin is None:
            origin = (0,0,0)
        x = LineSegmentsGeometry(positions=[[origin, self._shift(origin, [length, 0, 0])]])
        y = LineSegmentsGeometry(positions=[[origin, self._shift(origin, [0, length, 0])]])
        z = LineSegmentsGeometry(positions=[[origin, self._shift(origin, [0, 0, length])]])

        mx = LineMaterial(linewidth=width, color='red')
        my = LineMaterial(linewidth=width, color='green')
        mz = LineMaterial(linewidth=width, color='blue')

        self.axes = [LineSegments2(x, mx), LineSegments2(y, my), LineSegments2(z, mz)]

    def _shift(self, v, offset):
        return [x+o for x, o in zip(v, offset)]

    def toggleAxes(self, change):
        for i in range(3):
            self.axes[i].visible = change