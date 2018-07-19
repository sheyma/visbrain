"""Create and control a 3D object.

This class can be used to create a 3D object, based on vertices and faces. It
can be used to create the main brain or areas (like brodmann / gyrus). This
class is also responsible of turning camera rotations into light ajustement.

This class inherit from vispy.visuals so it can be turned into a vispy node,
which make it easier to add vispy transformations.

Authors: Etienne Combrisson <e.combrisson@gmail.com>

Textures
--------
1D texture : white (0) + sulcus (.5) + mask (1.)
2D texture : overlays (limited to 4 overlays)

License: BSD (3-clause)
"""
import numpy as np
import logging

from vispy import gloo
from vispy.visuals import Visual
import vispy.visuals.transforms as vist
from vispy.scene.visuals import create_visual_node

from visbrain.utils import (Colormap, color2vb, convert_meshdata,
                            wrap_properties, normalize)


logger = logging.getLogger('visbrain')

# Light and color properties :
LUT_LEN = 1024
LIGHT_POSITION = [100.] * 3
LIGHT_INTENSITY = [1.] * 3
COEF_AMBIENT = .05
COEF_SPECULAR = 0.
SULCUS_COLOR = [.4] * 3 + [1.]

# Vertex shader : executed code for individual vertices. The transformation
# applied to each one of them is the camera rotation.
VERT_SHADER = """
#version 120
varying vec3 v_position;
varying vec3 v_normal;
varying vec4 v_color;

void main() {
    v_position = $a_position;
    v_normal = $a_normal;

    // Compute background color (i.e white / mask / sulcus)
    vec4 bg_color = texture1D($u_bgd_text, $a_bgd_data);

    // Compute overlay colors :
    vec4 overlay_color = vec4(0., 0., 0., 0.);
    float u_div = 0.;
    float off = float($u_n_overlays > 1) * 0.999999;
    for (int i=0; i<$u_n_overlays; i++) {
        // Texture coordinate :
        vec2 tex_coords = vec2($u_range[i], (i + off)/$u_n_overlays);
        // Get the color using the texture :
        vec4 ux = texture2D($u_over_text, tex_coords);
        // Ponderate the color with transparency level :
        overlay_color += $u_alphas[i] * ux;
        // Number of contributing overlay per vertex :
        u_div += $u_alphas[i];
    }
    overlay_color /= max(u_div, 1.);

    // Mix background and overlay colors :
    v_color = mix(bg_color, overlay_color, overlay_color.a);

    // Finally apply camera transform to position :
    gl_Position = $transform(vec4($a_position, 1));
}
"""


# Fragment shader : executed code to each Fragment generated by the
# Rasterization and turn it into a set of colors and a single depth value.
# The code bellow generate three types of light :
# * Ambient : uniform light across fragments
# * Diffuse : ajust light according to normal vector
# * Specular : add some high-density light for a "pop / shiny" effect.
FRAG_SHADER = """
#version 120
varying vec3 v_position;
varying vec4 v_color;
varying vec3 v_normal;

void main() {

    // ----------------- Ambient light -----------------
    vec3 ambientLight = $u_coef_ambient * v_color.rgb * $u_light_intensity;

    // ----------------- Diffuse light -----------------
    // Calculate the vector from this pixels surface to the light source
    vec3 surfaceToLight = $u_light_position - v_position;

    // Calculate the cosine of the angle of incidence
    float l_surf_norm = length(surfaceToLight) * length(v_normal);
    float brightness = dot(v_normal, surfaceToLight) / l_surf_norm;
    // brightness = clamp(brightness, 0, 1);
    brightness = max(min(brightness, 1.0), 0.0);

    // Get diffuse light :
    vec3 diffuseLight =  v_color.rgb * brightness * $u_light_intensity;

    // ----------------- Specular light -----------------
    vec3 surfaceToCamera = vec3(0.0, 0.0, 1.0) - v_position;
    vec3 K = normalize(normalize(surfaceToLight) + normalize(surfaceToCamera));
    float specular = clamp(pow(abs(dot(v_normal, K)), 40.), 0.0, 1.0);
    specular *= $u_coef_specular;
    vec3 specularLight = specular * vec3(1., 1., 1.) * $u_light_intensity;

    // ----------------- Attenuation -----------------
    // float att = 0.0001;
    // float distanceToLight = length($u_light_position - v_position);
    // float attenuation = 1.0 / (1.0 + att * pow(distanceToLight, 2));

    // ----------------- Linear color -----------------
    // Without attenuation :
    vec3 linearColor = ambientLight + specularLight + diffuseLight;

    // With attenuation :
    // vec3 linearColor = attenuation*(specularLight + diffuseLight);
    // linearColor += ambientLight

    // ----------------- Gamma correction -----------------
    // vec3 gamma = vec3(1.0/1.2);

    // ----------------- Final color -----------------
    // Without gamma correction :
    gl_FragColor = vec4(linearColor, $u_alpha);

    // With gamma correction :
    // gl_FragColor = vec4(pow(linearColor, gamma), $u_alpha);
}
"""


class BrainVisual(Visual):
    """Visual object for brain mesh.

    The brain visual color rndering use threen levels :

        * 0. : default brain color (white)
        * 1. : custom colors (e.g projection, activation...)
        * 2. : uniform mask color (e.g non-significant p-values...)

    Parameters
    ----------
    vertices : array_like | None
        Vertices to set of shape (N, 3) or (M, 3)
    faces : array_like | None
        Faces to set of shape (M, 3)
    normals : array_like | None
        The normals to set (same shape as vertices)
    camera : vispy | None
        Add a camera to the mesh. This object must be a vispy edfault
        camera.
    meshdata : vispy.meshdata | None
        Custom vispy mesh data
    hemisphere : string | 'both'
        Choose if an hemisphere has to be selected ('both', 'left', 'right')
    lr_index : int | None
        Integer which specify the index where to split left and right
        hemisphere.
    """

    def __len__(self):
        """Return the number of vertices."""
        return self._vertices.shape[0]

    def __iter__(self):
        """Iteration function."""
        pass

    def __getitem__(self):
        """Get a specific item."""
        pass

    def __init__(self, vertices=None, faces=None, normals=None, lr_index=None,
                 hemisphere='both', sulcus=None, alpha=1., mask_color='orange',
                 camera=None, meshdata=None, invert_normals=False):
        """Init."""
        self._camera = None
        self._camera_transform = vist.NullTransform()
        self._translucent = True
        self._alpha = alpha
        self._hemisphere = hemisphere
        self._n_overlay = 0
        self._data_lim = []

        # Initialize the vispy.Visual class with the vertex / fragment buffer :
        Visual.__init__(self, vcode=VERT_SHADER, fcode=FRAG_SHADER)

        # _________________ BUFFERS _________________
        # Vertices / faces / normals / color :
        def_3 = np.zeros((0, 3), dtype=np.float32)
        self._vert_buffer = gloo.VertexBuffer(def_3)
        self._normals_buffer = gloo.VertexBuffer(def_3)
        self._bgd_buffer = gloo.VertexBuffer()
        self._xrange_buffer = gloo.VertexBuffer()
        self._alphas_buffer = gloo.VertexBuffer()
        self._index_buffer = gloo.IndexBuffer()

        # _________________ PROGRAMS _________________
        self.shared_program.vert['a_position'] = self._vert_buffer
        self.shared_program.vert['a_normal'] = self._normals_buffer
        self.shared_program.vert['u_n_overlays'] = self._n_overlay
        self.shared_program.frag['u_alpha'] = alpha

        # _________________ LIGHTS _________________
        self.shared_program.frag['u_light_intensity'] = LIGHT_INTENSITY
        self.shared_program.frag['u_coef_ambient'] = COEF_AMBIENT
        self.shared_program.frag['u_coef_specular'] = COEF_SPECULAR

        # _________________ DATA / CAMERA / LIGHT _________________
        self.set_data(vertices, faces, normals, hemisphere, lr_index,
                      invert_normals, sulcus, meshdata)
        self.set_camera(camera)
        self.mask_color = mask_color

        # _________________ GL STATE _________________
        self.set_gl_state('translucent', depth_test=True, cull_face=False,
                          blend=True, blend_func=('src_alpha',
                                                  'one_minus_src_alpha'))
        self._draw_mode = 'triangles'
        self.freeze()

    # =======================================================================
    # =======================================================================
    # Set data / light / camera / clean
    # =======================================================================
    # =======================================================================
    def set_data(self, vertices=None, faces=None, normals=None,
                 hemisphere='both', lr_index=None, invert_normals=False,
                 sulcus=None, meshdata=None):
        """Set data to the mesh.

        Parameters
        ----------
        vertices : ndarray | None
            Vertices to set of shape (N, 3) or (M, 3)
        faces : ndarray | None
            Faces to set of shape (M, 3)
        normals : ndarray | None
            The normals to set (same shape as vertices)
        meshdata : vispy.meshdata | None
            Custom vispy mesh data
        hemisphere : string | 'both'
            Choose if an hemisphere has to be selected ('both', 'left',
            'right')
        invert_normals : bool | False
            Sometimes it appear that the brain color is full
            black. In that case, turn this parameter to True
            in order to invert normals.
        """
        # ____________________ VERTICES / FACES / NORMALS ____________________
        vertices, faces, normals = convert_meshdata(vertices, faces, normals,
                                                    meshdata, invert_normals)
        self._vertices = vertices
        self._faces = faces
        self._normals = normals
        # Keep shapes :
        self._shapes = np.zeros(1, dtype=[('vert', int), ('faces', int)])
        self._shapes['vert'] = vertices.shape[0]
        self._shapes['faces'] = faces.shape[0]

        # Find ratio for the camera :
        v_max, v_min = vertices.max(0), vertices.min(0)
        cam_center = (v_max + v_min).astype(float) / 2.
        cam_scale_factor = (v_max - v_min).astype(float)
        self._opt_cam_state = dict(center=cam_center,
                                   scale_factor=cam_scale_factor)
        logger.debug("Optimal camera state : %r" % self._opt_cam_state)

        # ____________________ HEMISPHERE ____________________
        if lr_index is None:
            logger.debug('Left/Right hemispheres inferred from verices')
            lr_index = vertices[:, 0] <= vertices[:, 0].mean()
        self._lr_index = lr_index.astype(bool)

        # ____________________ BUFFERS ____________________
        # Vertices // faces // normals :
        self._vert_buffer.set_data(vertices, convert=True)
        self._normals_buffer.set_data(normals, convert=True)
        self.hemisphere = hemisphere
        # Sulcus :
        n = len(self)
        sulcus = np.zeros((n,), dtype=bool) if sulcus is None else sulcus
        assert isinstance(sulcus, np.ndarray)
        assert len(sulcus) == n and sulcus.dtype == bool

        # ____________________ TEXTURES ____________________
        # Background texture :
        self._bgd_data = np.zeros((n,), dtype=np.float32)
        self._bgd_data[sulcus] = .9
        self._bgd_buffer.set_data(self._bgd_data, convert=True)
        self.shared_program.vert['a_bgd_data'] = self._bgd_buffer
        # Overlay texture :
        self._text2d_data = np.zeros((2, LUT_LEN, 4), dtype=np.float32)
        self._text2d = gloo.Texture2D(self._text2d_data)
        self.shared_program.vert['u_over_text'] = self._text2d
        # Build texture range :
        self._xrange = np.zeros((n, 2), dtype=np.float32)
        self._xrange_buffer.set_data(self._xrange)
        self.shared_program.vert['u_range'] = self._xrange_buffer
        # Define buffer for transparency per overlay :
        self._alphas = np.zeros((n, 2), dtype=np.float32)
        self._alphas_buffer.set_data(self._alphas)
        self.shared_program.vert['u_alphas'] = self._alphas_buffer

    def add_overlay(self, data, vertices=None, to_overlay=None, mask_data=None,
                    **kwargs):
        """Add an overlay to the mesh.

        Note that the current implementation limit to a number of of four
        overlays.

        Parameters
        ----------
        data : array_like
            Array of data of shape (n_data,).
        vertices : array_like | None
            The vertices to color with the data of shape (n_data,).
        to_overlay : int | None
            Add data to a specific overlay. This parameter must be a integer.
        mask_data : array_like | None
            Array to specify if some vertices have to be considered as masked
            (and use the `mask_color` color)
        kwargs : dict | {}
            Additional color color properties (cmap, clim, vmin, vmax, under,
            over, translucent)
        """
        # Check input variables :
        if vertices is None:
            vertices = np.ones((len(self),), dtype=bool)
        data = np.asarray(data)
        to_overlay = self._n_overlay if to_overlay is None else to_overlay
        data_lim = (data.min(), data.max())
        if len(self._data_lim) < to_overlay + 1:
            self._data_lim.append(data_lim)
        else:
            self._data_lim[to_overlay] = data_lim
        # -------------------------------------------------------------
        # TEXTURE COORDINATES
        # -------------------------------------------------------------
        need_reshape = to_overlay >= self._xrange.shape[1]
        if need_reshape:
            # Add column of zeros :
            z_ = np.zeros((len(self),), dtype=np.float32)
            z_text = np.zeros((1, LUT_LEN, 4), dtype=np.float32)
            self._xrange = np.c_[self._xrange, z_]
            self._alphas = np.c_[self._alphas, z_]
            self._text2d_data = np.concatenate((self._text2d_data, z_text))
        # (x, y) coordinates of the overlay for the texture :
        self._xrange[vertices, to_overlay] = normalize(data)
        # Transparency :
        self._alphas[vertices, to_overlay] = 1.  # transparency level

        # -------------------------------------------------------------
        # TEXTURE COLOR
        # -------------------------------------------------------------
        # Colormap interpolation (if needed):
        colormap = Colormap(**kwargs)
        vec = np.linspace(data_lim[0], data_lim[1], LUT_LEN)
        self._text2d_data[to_overlay, ...] = colormap.to_rgba(vec)
        # Send data to the mask :
        if isinstance(mask_data, np.ndarray) and len(mask_data) == len(self):
            self._bgd_data[mask_data] = .5
            self._bgd_buffer.set_data(self._bgd_data)
        # -------------------------------------------------------------
        # BUFFERS
        # -------------------------------------------------------------
        if need_reshape:
            # Re-define buffers :
            self._xrange_buffer = gloo.VertexBuffer(self._xrange)
            self._text2d = gloo.Texture2D(self._text2d_data)
            self._alphas_buffer = gloo.VertexBuffer(self._alphas)
            # Send buffers to vertex shader :
            self.shared_program.vert['u_range'] = self._xrange_buffer
            self.shared_program.vert['u_alphas'] = self._alphas_buffer
            self.shared_program.vert['u_over_text'] = self._text2d
        else:
            self._xrange_buffer.set_data(self._xrange)
            self._text2d.set_data(self._text2d_data)
            self._alphas_buffer.set_data(self._alphas)
        # Update the number of overlays :
        self._n_overlay = to_overlay + 1
        self.shared_program.vert['u_n_overlays'] = self._n_overlay

    def update_colormap(self, to_overlay=None, **kwargs):
        """Update colormap properties of an overlay.

        Parameters
        ----------
        to_overlay : int | None
            Add data to a specific overlay. This parameter must be a integer.
            If no overlay is specified, the colormap of the last one is used.
        kwargs : dict | {}
            Additional color color properties (cmap, clim, vmin, vmax, under,
            over, translucent)
        """
        if self._n_overlay >= 1:
            overlay = self._n_overlay - 1 if to_overlay is None else to_overlay
            # Define the colormap data :
            data_lim = self._data_lim[overlay]
            col = np.linspace(data_lim[0], data_lim[1], LUT_LEN)
            self._text2d_data[overlay, ...] = Colormap(**kwargs).to_rgba(col)
            self._text2d.set_data(self._text2d_data)
            self.update()

    def set_camera(self, camera=None):
        """Set a camera to the mesh.

        This is essential to add to the mesh the link between the camera
        rotations (transformation) to the vertex shader.

        Parameters
        ----------
        camera : vispy.camera | None
            Set a camera to the Mesh for light adaptation
        """
        if camera is not None:
            self._camera = camera
            self._camera_transform = self._camera.transform
            self.update()

    def clean(self):
        """Clean the mesh.

        This method delete the object from GPU memory.
        """
        # Delete vertices / faces / colors / normals :
        self._vert_buffer.delete()
        self._index_buffer.delete()
        self._normals_buffer.delete()
        self._xrange_buffer.delete()
        self._math_buffer.delete()

    def _build_bgd_texture(self):
        color_1d = np.c_[np.array([1.] * 4), np.array(self.mask_color),
                         np.array(SULCUS_COLOR)].T
        text_1d = gloo.Texture1D(color_1d.astype(np.float32))
        self.shared_program.vert['u_bgd_text'] = text_1d

    # =======================================================================
    # =======================================================================
    # Drawing functions
    # =======================================================================
    # =======================================================================

    def draw(self, *args, **kwds):
        """Call when drawing only."""
        Visual.draw(self, *args, **kwds)

    def _prepare_draw(self, view=None):
        """Call everytime there is an interaction with the mesh."""
        view_frag = view.view_program.frag
        view_frag['u_light_position'] = self._camera_transform.map(
            LIGHT_POSITION)[0:-1]

    @staticmethod
    def _prepare_transforms(view):
        """First rendering call."""
        tr = view.transforms
        transform = tr.get_transform()

        view_vert = view.view_program.vert
        view_vert['transform'] = transform

    # =======================================================================
    # =======================================================================
    # Properties
    # =======================================================================
    # =======================================================================

    # ----------- HEMISPHERE -----------
    @property
    def hemisphere(self):
        """Get the hemisphere value."""
        return self._hemisphere

    @hemisphere.setter
    def hemisphere(self, value):
        """Set hemisphere value."""
        assert value in ['left', 'both', 'right']
        if value == 'both':
            index = self._faces
        elif value == 'left':
            index = self._faces[self._lr_index[self._faces[:, 0]], :]
        elif value == 'right':
            index = self._faces[~self._lr_index[self._faces[:, 0]], :]
        self._index_buffer.set_data(index)
        self.update()
        self._hemisphere = value

    # ----------- SULCUS -----------
    @property
    def sulcus(self):
        """Get the sulcus value."""
        pass
        # return self._sulcus

    @sulcus.setter
    def sulcus(self, value):
        """Set sulcus value."""
        assert isinstance(value, np.ndarray) and len(value) == len(self)
        assert isinstance(value.dtype, bool)
        self._bgd_data[value] = 1.
        self._bgd_buffer.set_data(self._bgd_data)
        self.update()

    # ----------- TRANSPARENT -----------
    @property
    def translucent(self):
        """Get the translucent value."""
        return self._translucent

    @translucent.setter
    @wrap_properties
    def translucent(self, value):
        """Set translucent value."""
        assert isinstance(value, bool)
        if value:
            self.set_gl_state('translucent', depth_test=False, cull_face=False)
            alpha = 0.1
        else:
            self.set_gl_state('translucent', depth_test=True, cull_face=False)
            alpha = 1.
        self._translucent = value
        self.alpha = alpha
        self.update_gl_state()

    # ----------- ALPHA -----------
    @property
    def alpha(self):
        """Get the alpha value."""
        return self._alpha

    @alpha.setter
    @wrap_properties
    def alpha(self, value):
        """Set alpha value."""
        assert isinstance(value, (int, float))
        value = min(value, .1) if self._translucent else 1.
        self._alpha = value
        self.shared_program.frag['u_alpha'] = value
        self.update()

    # ----------- MASK_COLOR -----------
    @property
    def mask_color(self):
        """Get the mask_color value."""
        return self._mask_color

    @mask_color.setter
    @wrap_properties
    def mask_color(self, value):
        """Set mask_color value."""
        value = color2vb(value).ravel()
        self._mask_color = value
        self._build_bgd_texture()

    @property
    def minmax(self):
        """Get the data limits value."""
        return self._data_lim[self._n_overlay - 1]


BrainMesh = create_visual_node(BrainVisual)
