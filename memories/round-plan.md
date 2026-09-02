Gap: The agent cannot inspect GeoTIFF and other GDAL raster imagery, so it cannot answer tasks about raster pixels, bands, CRS, or georeferenced extents.
Change: Add a Rasterio-backed inspect_raster tool with bounded single-band window reads and raster metadata.
Priority: This is an unclosed input-format gap distinct from already-supported vector geodata and scientific HDF5, and hard geospatial tasks commonly use raster products.
