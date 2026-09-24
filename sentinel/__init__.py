"""sentinel-pod: error capture, silent-bug (contract) detection, and ingest.

`capture.py`, `middleware.py` and `logging_handler.py` are the "drop-in" bits
imported BY a monitored app (apps/target_app); everything else runs only
inside the sentinel-pod process itself.
"""
