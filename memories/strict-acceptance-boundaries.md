# Strict acceptance boundaries in probe tasks

Probe instructions now require an acceptance-boundary table before implementing exact parsers, validators, selectors, or extractors. They distinguish permitted normalization from convenient broader helpers, require nearest invalid neighbours, account for decoding already performed by structural parsers, cover equivalent syntax variants, and turn those distinctions into checks against the public entry point or deliverable. `tests/test_probe_acceptance_boundaries.py` binds these cases to the generated probe prompt.
