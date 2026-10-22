'''Import PXS lab-source (UDKM) scan folders into nx5d repo format.

The package is deliberately import-light: nothing here pulls ``nx5d``
(reached only as the ``pm`` subprocess), ``xraydc`` (reached only as a
string in a spice record) or ``s3fs`` (imported inside the functions of
:mod:`pxs_import.s3` that need it).
'''
