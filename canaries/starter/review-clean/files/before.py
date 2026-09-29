def bucket(age):
    if age < 0:
        raise ValueError("negative age")
    if age <= 12:
        return "child"
    if age <= 64:
        return "adult"
    return "senior"
