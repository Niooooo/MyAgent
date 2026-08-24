def build_cache_key(namespace, parts):
    return namespace + ":" + ":".join(parts)
