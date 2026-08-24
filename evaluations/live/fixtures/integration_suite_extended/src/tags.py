def normalize_tags(tags):
    while "" in tags:
        tags.remove("")
    return tags
