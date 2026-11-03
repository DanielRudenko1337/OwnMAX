import secrets


def issue_code(phone):
    code = f"{secrets.randbelow(1000000):06d}"
    print(f"OwnMAX: код входа или регистрации для {phone}: {code}", flush=True)
    return code
