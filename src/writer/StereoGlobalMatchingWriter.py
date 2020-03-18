
import os
from math import tan

import bpy
import cv2
import numpy as np

from src.utility.SGMUtility import fill_in_fast
from src.main.Module import Module
from src.utility.BlenderUtility import load_image
from src.utility.SGMUtility import resize
from src.renderer.Renderer import Renderer


class StereoGlobalMatchingWriter(Module):
    """ Writes depth image generated from the stereo global matching algorithm to file

    **Configuration**:

    .. csv-table::
       :header: "Parameter", "Description"
       "infer_focal_length_from_fov", "If true, then focal length would be calculated from the field of view angle, otherwise the value of the focal length would be read from the config parameter: "focal_length". Type: bool. Optional. Default value: False"
       "disparity_filter", "Applies post-processing of the generated disparity map using WLS filter. Type: bool. Optional. Default value: True"
       "depth_completion", "Applies basic depth completion using image processing techniques. Type: bool. Optional. Default value: True"
       "focal_length", "Focal length used in the depth calculation step, should be set if 'infer_focal_length_from_fov' is set to false. Type: float. Optional. Default value: 0.0"

       "window_size", "Semi-global matching kernel size. Should be an odd number. Type: int. Optional. Default value: 7"
       "num_disparities", "Semi-global matching number of disparities. Should be > 0 and divisible by 16. Type: int. Optional. Default value: 32"
       "min_disparity", "Semi-global matching minimum disparity. Type: int. Optional. Default value: 0"

       "output_disparity", "Additionally outputs the disparity map. Type: bool. Optional. Default value: False"
    """

    def __init__(self, config):
        Module.__init__(self, config)

        self.rgb_output_key = self.config.get_string("rgb_output_key", "colors")
        if self.rgb_output_key is None:
            raise Exception("RGB output is not registered, please register the RGB renderer before this module.")

        self.output_dir = self._determine_output_dir(False)
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

        self.depth_max = Renderer.DEPTH_END

    # https://elib.dlr.de/73119/1/180Hirschmueller.pdf
    def sgm(self, imgL, imgR):
        window_size = self.config.get_int("window_size", 7)
        if window_size % 2 == 0:
            raise Exception("Window size must be an odd number")

        numDisparities = self.config.get_int("num_disparities", 32)
        if not (numDisparities > 0 and numDisparities % 16 == 0):
            raise Exception("Number of disparities must be > 0 and divisible by 16")

        left_matcher = cv2.StereoSGBM_create(
            minDisparity=self.config.get_int("min_disparity", 0),
            numDisparities=numDisparities,
            blockSize=5,
            P1=8 * 3 * window_size ** 2,
            P2=32 * 3 * window_size ** 2,
            disp12MaxDiff=-1,
            uniquenessRatio=15,
            speckleWindowSize=0,
            speckleRange=2,
            preFilterCap=63,
            # mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY
            mode=cv2.StereoSGBM_MODE_HH
        )

        if self.config.get_bool("disparity_filter", True):
            right_matcher = cv2.ximgproc.createRightMatcher(left_matcher)

            lmbda = 80000
            sigma = 1.2

            wls_filter = cv2.ximgproc.createDisparityWLSFilter(matcher_left=left_matcher)
            wls_filter.setLambda(lmbda)
            wls_filter.setSigmaColor(sigma)

            dispr = right_matcher.compute(imgR, imgL)
            dispr = np.int16(dispr)

        displ = left_matcher.compute(imgL, imgR)
        displ = np.int16(displ)

        filteredImg = None
        if self.config.get_bool("disparity_filter", True):
            filteredImg = wls_filter.filter(displ, imgL, None, dispr).astype(np.float32)
            filteredImg = cv2.normalize(src=filteredImg, dst=filteredImg, beta=0, alpha=255, norm_type=cv2.NORM_MINMAX)

        disparity = np.float64(filteredImg) / 16.0 if self.config.get_bool("disparity_filter", True) else \
            np.float64(displ) / 16.0

        # Crop and resize, due to baseline, a part of the image on the left can't be matched with the one on the right
        disparity = resize(disparity[:, numDisparities:], (self.width, self.height))

        # Triangulation
        depth = (1.0 / disparity) * self.baseline * self.focal_length

        # Clip from depth map to 25 meters
        depth[depth > self.depth_max] = self.depth_max
        depth[depth < 0] = 0.0

        if self.config.get_bool("depth_completion", True):
            depth = fill_in_fast(depth, self.depth_max)

        
        disparity = np.int16(disparity)
        return depth, disparity

    def run(self):
        self.rgb_output_path = self._find_registered_output_by_key(self.rgb_output_key)["path"]

        cam = bpy.context.scene.camera.data

        self.width = bpy.context.scene.render.resolution_x
        self.height = bpy.context.scene.render.resolution_y

        self.baseline = cam.stereo.interocular_distance
        if not self.baseline:
            raise Exception(
                "Stereo parameters are not set. Make sure to enable RGB stereo rendering before this module.")

        if self.config.get_bool("infer_focal_length_from_fov", False):
            fov = cam.angle_x if cam.angle_x else cam.angle
            if not fov:
                raise Exception("Could not obtain field of view angle")
            self.focal_length = float((1.0 / tan(fov / 2.0)) * (float(self.width) / 2.0))
        else:
            self.focal_length = self.config.get_float("focal_length", 0.0)
            if self.focal_length == 0.0:
                raise Exception(
                    "Focal length set to 0. This is either intentional or because no value was set by the user. Either way, this needs to be corrected by setting a value > 0 or enabling 'infer_focal_length_from_fov'.")

        for frame in range(bpy.context.scene.frame_start, bpy.context.scene.frame_end):
            path_split = self.rgb_output_path.split(".")
            path_l = "{}_L.{}".format(path_split[0], path_split[1])
            path_r = "{}_R.{}".format(path_split[0], path_split[1])

            imgL = load_image(path_l % frame)
            imgR = load_image(path_r % frame)

            depth, disparity = self.sgm(imgL, imgR)

            np.savez_compressed(os.path.join(self.output_dir, "stereo-depth_%04d") % frame, depth=depth)

            if self.config.get_bool("output_disparity", False):
                np.savez_compressed(os.path.join(self.output_dir, "disparity_%04d") % frame, disparity=disparity)                