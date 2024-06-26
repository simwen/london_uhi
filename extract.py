# Load required libs
from landsatxplore.api import API
from landsatxplore.earthexplorer import EarthExplorer
import os
import pandas as pd
import tarfile
import matplotlib.pyplot as plt
import rasterio as rio
from rasterio.enums import Resampling
from rasterio.warp import calculate_default_transform, reproject, Resampling
from rasterio.plot import show
import geopandas as gpd
from rasterio.mask import mask
import sys

def run(username, password, today, ALL_RUNS, RAW_DIR, INT_DIR):

    print("##################################################")
    print("2: Starting extract.py")
    print("##################################################")

    ## 1. Find and save the data --------------------------------------------------

    # Initialize a new API instance and get an access key
    api = API(username, password)

    # Search for Landsat scenes
    scenes = api.search(
        dataset='landsat_ot_c2_l2',        # name of satellite and collection/level
        latitude=51.509, longitude=-0.126, # coords of London
        start_date='2023-04-01',           # start of date range
        end_date='2023-10-01',             # start of date range
        max_cloud_cover=10                 # filter max % cloud cover
    )

    print(f"{len(scenes)} scenes found")
    api.logout()

    # Create df of scenes
    df_scenes = pd.DataFrame(scenes)
    df_scenes = df_scenes[['display_id','wrs_path', 'spatial_coverage','satellite','cloud_cover','acquisition_date']]
    distinct_scenes = df_scenes.drop_duplicates(subset='spatial_coverage')

    # Load list of lsoas/lads
    ## https://geoportal.statistics.gov.uk/datasets/56dfa94d126548b48ceb7bfdd67fb11e
    lsoa_lookup = pd.read_csv(os.path.join(ALL_RUNS, 'area_lookup.csv'))
    lsoa_lookup = lsoa_lookup[['LSOA21CD', 'LEP21NM1', 'LAD22CD', 'LAD22NM']]
    lsoa_list = lsoa_lookup[lsoa_lookup['LEP21NM1'] == 'London']

    # Define lsoa list
    lsoa_list = lsoa_list.drop_duplicates()

    # Define lad list
    lad_list = lsoa_list[['LEP21NM1','LAD22CD','LAD22NM']].drop_duplicates()

    # Load UK LADS BNG CRS 
    ## https://geoportal.statistics.gov.uk/datasets/127c4bda06314409a1fa0df505f510e6_0/explore
    # uk_lads = gpd.read_file(os.path.join(ALL_RUNS, 'LAD_Dec_2017_FEB_in_Great_Britain.shp'))
    uk_lads = gpd.read_file(os.path.join(ALL_RUNS, 'LAD_Dec_2023_Boundaries', 'LAD_DEC_2023_UK_BFC.shp'))

    # Join London Lads to shapefile
    ldn_lads = pd.merge(lad_list, uk_lads, how = 'left', left_on='LAD22CD',right_on='LAD23CD')

    # Create outline of London by removing LADs
    ldn_lads = gpd.GeoDataFrame(ldn_lads,geometry='geometry')
    ldn_outline = ldn_lads.dissolve(by='LEP21NM1')[['geometry']]

    # convert ldn_outline crs to enable cropping
    ldn_outline.set_crs(epsg=27700, inplace=True)
    ldn_outline.to_csv(os.path.join(ALL_RUNS, 'london_outline_bng.csv'))
    ldn_outline = ldn_outline.to_crs(epsg=4326)

    ## 2. Convert each pane's CRS to BNG -----------------------------------------------

    def convert_crs(output_crs, output_filename):
        """
        Returns new reprojected file with new CRS.

            Parameters:
                output_crs (str): The EPSG crs code to convert the file to e.g. EPSG:4326
                output_filename (str): What to save the output filename as
                        
            Returns:
                ...
        """
        output_raster_path = os.path.join(INT_DIR, f'{output_filename}.tif')

        # Open the input raster file
        with rio.open(os.path.join(RAW_DIR,f'{pane_id}/{pane_id}_ST_B10.TIF')) as src:
            
            # Get the transform and dimensions for the reprojected raster
            transform, width, height = calculate_default_transform(     
                src.crs, output_crs, src.width, src.height, *src.bounds)

            # Set up the reprojected dataset
            kwargs = src.meta.copy()
            kwargs.update({
                'crs': output_crs,
                'transform': transform,
                'width': width,
                'height': height
            })

            # Create the output raster file with new EPSG
            with rio.open(output_raster_path, 'w', **kwargs) as dst:
                # Reproject the data from the source CRS to the target CRS
                reproject(
                    source=rio.band(src, 1),
                    destination=rio.band(dst, 1),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=output_crs,
                    resampling=Resampling.nearest)


    ## 3. Download the files and convert crs ---------------------------------
    # Initialize the API
    ee = EarthExplorer(username, password)

    # Download and extract the scenes 
    for index, row in df_scenes.iterrows():
        pane_id = row['display_id']
        
        print(f'Starting extracting scene {index} / {len(df_scenes)}')

        try: 
            ee.download(pane_id, output_dir= RAW_DIR)
            print('{} succesful'.format(pane_id))
            
        # Additional error handling
        except:
            if os.path.isfile(os.path.join(RAW_DIR, f'{pane_id}.tar')):
                print(f'{pane_id} error but file exists')
            else:
                print(f'{pane_id} error')
        

        tar_file_path = os.path.join(RAW_DIR, f'{pane_id}.tar')
        tif_directory = os.path.join(RAW_DIR, pane_id)

        # Create the directory if it doesn't exist
        os.makedirs(tif_directory, exist_ok=True)

        # Extract ST_B10 tif
        with tarfile.open(tar_file_path, 'r') as tar:
            # Loop through files in tar archive
            for member in tar.getmembers():
                if member.name.endswith('ST_B10.TIF'):
                    tar.extract(member, tif_directory)
        
        # Convert to CRS of extract tif to BNG
        convert_crs(output_crs = 'EPSG:27700', output_filename = f'bng_{pane_id}')

    ee.logout()

    print('2. Finished tif download and extraction')


    ## 4. Crop to London square -----------------------------------------------

    # Check if each scene fully covers London
    mask = df_scenes['spatial_coverage'].apply(lambda x: not any(x.contains(y) for y in ldn_outline['geometry']))

    # Drop scenes which don't meet criteria
    df_scenes_filtered = df_scenes[~mask]

    df_scenes_filtered.to_csv(os.path.join(RAW_DIR, "df_scenes_filtered.csv"))

    print(f'df_scenes_filtered has shape: {df_scenes_filtered.shape}')
   
    # Load england LSOAs
    ## https://geoportal.statistics.gov.uk/datasets/bb427d36197443959de8a1462c8f1c55_0/explore
    england_lsoas_path = os.path.join(ALL_RUNS, 'LSOA_2021_EW_BFC_V8.shp')
    england_lsoas = gpd.read_file(england_lsoas_path)
    london_lsoas_lads_bng = pd.merge(england_lsoas, lsoa_list, on='LSOA21CD', how='left')

    # Filter rows where 'LEP' is 'London'
    london_lsoas_lads_bng = london_lsoas_lads_bng[london_lsoas_lads_bng['LEP21NM1'] == 'London']
    london_lsoas_lads_bng.to_csv(os.path.join(ALL_RUNS, 'london_lsoas_lads_bng.csv'))

    # Crop tif to square
    maxx = london_lsoas_lads_bng['geometry'].bounds['maxx'].max()
    maxy = london_lsoas_lads_bng['geometry'].bounds['maxy'].max()
    minx = london_lsoas_lads_bng['geometry'].bounds['minx'].min()
    miny = london_lsoas_lads_bng['geometry'].bounds['miny'].min()

    #minx, miny, maxx, maxy = 500000, 150000, 565000, 205000

    for index, row in df_scenes_filtered.iterrows():
        
        pane_id = row['display_id']
        print(f'cropping {pane_id}')
        with rio.open(os.path.join(INT_DIR,f'bng_{pane_id}.tif')) as src:
        # Get the window corresponding to the specified bounding box
            window = rio.windows.from_bounds(minx, miny, maxx, maxy, src.transform)
        
            # Read the data within the specified window
            cropped_data = src.read(window=window)
        
        # Create a new rasterio DatasetReader for the cropped data
        cropped_src = rio.open(
            os.path.join(INT_DIR,f'bng_{pane_id}.tif'),
            'w',
            driver='GTiff',
            width=cropped_data.shape[2],
            height=cropped_data.shape[1],
            count=src.count,
            dtype=cropped_data.dtype,
            crs=src.crs,
            transform=src.window_transform(window)
        )
        
        # Write the cropped data to the new TIFF file
        cropped_src.write(cropped_data)
        
        # Close the files
        src.close()
        cropped_src.close()


    ## 5. Scale temp to celcius ------------------------------------------
    scale_factor = 0.00341802
    add_offset = 149
    kelvin_to_celsius = -273.15

    def scale_to_celcius(tif_name, plot = False):
        """
        """
        
        with rio.open(os.path.join(INT_DIR,f'{tif_name}.tif')) as cropped_src:

            # Read the surface temperature data (DN values)
            temperature_data_dn = cropped_src.read(1)

            # Convert DN to temperature in degrees Celsius
            temperature_data_celsius = (temperature_data_dn * scale_factor) + add_offset + kelvin_to_celsius
            
            if plot == True:
                # Show the plot with temperature in degrees Celsius
                plot = show(temperature_data_celsius, transform=cropped_src.transform, cmap='viridis', ax=plt.gca(), vmin=18)

                # Add a colorbar
                colorbar = plot.get_figure().colorbar(plot.get_images()[0], ax=plot.axes, orientation='vertical', pad=0.02)
                colorbar.set_label('Temperature (°C)')

                # Show the plot
                plt.show()
        
        with rio.open(os.path.join(INT_DIR,f'{tif_name}.tif')) as cropped_src:

            metadata = cropped_src.meta.copy()
            # Update the data type to float32 for temperature values
            metadata['dtype'] = 'float32'

            # Open the new file for writing
            with rio.open(os.path.join(INT_DIR,f'{tif_name}_celcius.tif'), 'w', **metadata) as dst:
                # Write the corrected temperature data to the new file
                dst.write(temperature_data_celsius, 1)

    for index, row in df_scenes_filtered.iterrows():
        
        pane_id = row['display_id']
        print(f'convert to celcius {pane_id}')
        scale_to_celcius(tif_name = f'bng_{pane_id}', plot = False)


    ## 6. Average temps ------------------------------------------

    def average_tifs():
        
        """
        Loads in each tif, sums up the values, then divides by length
        """
        
        summed_temps_array = None
        
        for index, row in df_scenes_filtered.iterrows():

            pane_id = row['display_id']
            print(f'summing {pane_id}')
        
            with rio.open(os.path.join(INT_DIR,f'bng_{pane_id}_celcius.tif')) as src:
                
                if summed_temps_array is None:
                    summed_temps_array = src.read(1)
                    
                # Check dimensions same as previous
                elif width == src.width and height == src.height:
                    summed_temps_array += src.read(1)
                    
                else:
                    print("Error - tifs different shapes")
                    sys.exit()
                
                # Set widths/heights
                width, height = src.width, src.height
        
        avg_temp = summed_temps_array / len(df_scenes_filtered)
        
        metadata = src.meta.copy()
        
        # Open the new file for writing
        with rio.open(os.path.join(INT_DIR,f'{today}_avg_celcius.tif'), 'w', **metadata) as dst:
            # Write the corrected temperature data to the new file
            dst.write(avg_temp, 1)
        
        return(avg_temp)

    import pdb
    try:
        avg_temp = average_tifs()
    except TypeError:
        pdb.post_mortem()

    print("##################################################")
    print("2: Finished extract.py")
    print("##################################################")

