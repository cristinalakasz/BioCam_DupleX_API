"""Layer 1 — .NET interop with the 3Brain BioCamDriver assemblies.

Nothing here can be executed on a machine without the BioCAM and the 3Brain
DLLs. Code in this layer is verified by reading, not by running: every .NET call
must be checked against API/3Brain.BioCamDriver.xml and the C# reference sample
before it reaches the lab. See the biocam-api-verifier agent.

This is the ONLY package permitted to import `clr` or `pythonnet`.

Empty in Phase 0. Populated by Phase 1.
"""
