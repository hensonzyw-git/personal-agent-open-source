"""Bound model-only web excerpts; keep stored evidence and source identities intact."""


def model_results(results):
    projected = []
    for result in results:
        if not isinstance(result, dict) or 'sources' not in result:
            projected.append(result)
            continue
        sources = []
        for source in result['sources']:
            # These fields are repeated aliases or host-only receipt metadata.
            item = {k: v for k, v in source.items()
                    if k not in {'ref', 'kind', 'provider_request_id'}}
            shortened = False
            for field, limit in (('title', 64), ('snippet', 96), ('content', 96)):
                value = item.get(field)
                if isinstance(value, str) and len(value.encode()) > limit:
                    item[field] = value.encode()[:limit].decode('utf-8', errors='ignore')
                    shortened = True
            item['truncated'] = bool(item.get('truncated') or shortened)
            sources.append(item)
        projected.append({**result, 'sources': sources})
    return projected
