"""BioCAM DupleX control and analysis.

The package is split into three layers by whether a laptop can prove the code
correct. This is not a stylistic preference; it is what allows development
without the instrument attached.

    biocam.interop   Layer 1  Calls the 3Brain .NET assemblies. NOT testable
                              without hardware. The only layer permitted to
                              import `clr` or `pythonnet`.

    biocam.data      Layer 2  Pure byte-and-number logic: payload decoding,
                              frame reassembly, unit conversion, gap detection.
                              Fully testable with synthetic buffers.

    biocam.analysis  Layer 3  Signal processing: spike detection, sorting.
                              Fully testable against recorded fixtures.

tests/test_no_hardware_imports.py enforces that Layers 2 and 3 never import the
interop layer's dependencies. If that test fails, the boundary has been broken.
"""
