import base64
import unittest
import cv2
import numpy as np
from activities.coc_client import CocError
from activities.coc_slider import slide_position


class SliderTests(unittest.TestCase):
    def test_recognizes_known_offset_with_rgb_and_rgba_patches(self):
        ground=np.random.default_rng(37).integers(0,256,(140,360,3),dtype=np.uint8)
        patch=ground[42:88,137:183].copy()
        encode=lambda img:base64.b64encode(cv2.imencode('.png',img)[1]).decode()
        for image in (patch,cv2.cvtColor(patch,cv2.COLOR_BGR2BGRA)):
            self.assertEqual(slide_position(encode(ground),encode(image)),137)

    def test_invalid_image_fails_with_a_clear_error(self):
        with self.assertRaises(CocError) as failure:
            slide_position('invalid','invalid')
        self.assertEqual(failure.exception.code,'SLIDER_RECOGNITION')


if __name__=='__main__':unittest.main()
