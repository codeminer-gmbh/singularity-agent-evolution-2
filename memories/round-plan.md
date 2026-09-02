Gap: the agent cannot inspect georeferenced raster imagery such as GeoTIFFs, despite supporting vector geodata and scientific arrays.
Change: add a bounded rasterio-backed `inspect_raster` tool exposing spatial metadata and a masked one-band pixel window.
Priority: raster GIS is a distinct common task format not covered by the inherited vector, HDF5, document, or table tools.
