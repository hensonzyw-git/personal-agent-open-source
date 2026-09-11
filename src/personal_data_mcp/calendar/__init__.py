"""Apple-calendar mirror: the ingest core and the query core.

The phone owns the calendar. These modules only decide what the mirror means:
`ingest_events` merges device-reported snapshots under `last_modified`
arbitration, and `query_events` reads the mirror back with honest freshness
markers. Neither contacts the device; the server's calendar sync route calls
the ingest, and `calendar.query_events` calls the query.
"""
