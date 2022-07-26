import unittest
import os
import rasterio
import geopandas as gpd


class TestCase(unittest.TestCase):

    def test_upper(self):
        self.assertEqual("foo".upper(), "FOO")

    def test_tiling(self):
        ### SEPILOK (East/West)
        #site_path = "/content/drive/Shareddrives/detectree2/data/Sepilok"
        site_path = "../data/sepilok"
        img_path = os.path.join(site_path, "RCD105_MA14_21_orthomosaic_20141023_reprojected_full_res.tif")
        crown_path = os.path.join(site_path, "crowns/SepilokEast.gpkg")
        # crown_path = os.path.join(site_path, "crowns/SepilokWest.gpkg")
     
        # Read in the tiff file
        data = rasterio.open(img_path)
        # Read in crowns (then filter by an attribute?)
        crowns = gpd.read_file(crown_path)
        crowns = crowns.to_crs(data.crs.data)

        # Set tiling parameters
        buffer = 15
        tile_width = 40
        tile_height = 40
        threshold = 0.5 
      
        from detectree2.preprocessing.tiling import tile_data_train
        
        out_dir = "./out/"
 
        tile_data_train(data, out_dir, buffer, tile_width, tile_height, crowns, threshold)
        
        return True

suite = unittest.TestLoader().loadTestsFromTestCase(TestCase)