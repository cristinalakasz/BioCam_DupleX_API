"""Layer 2 - the operator's window onto a recording.

Runs on the lab workstation, because the driver only runs on the machine
physically connected to the BioCAM. Operated by the on-site colleague first
and the author remotely later, so the stricter constraint governs: it has to
be usable by someone who did not write it and cannot ask questions
mid-experiment.

Nothing here imports the driver at module scope, so the whole window can be
opened, driven and tested on a machine with no BioCAM and no 3Brain DLLs -
see `biocam.ui.factories.ReplayFactory`. That is not a convenience. It is the
only way this code could be written at all from 600 km away, and it is how the
colleague can learn the controls before spending instrument time on them.
"""

from biocam.ui.controller import SessionController, SessionSnapshot

__all__ = ["SessionController", "SessionSnapshot"]
