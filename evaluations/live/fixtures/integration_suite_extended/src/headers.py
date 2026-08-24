def normalize_headers(headers):
    return {key.lower(): value for key, value in headers.items()}
