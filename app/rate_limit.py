from time import time

ATTEMPTS = {}

def check_rate_limit(ip: str, limit=5, window=60):
    now = time()
    ATTEMPTS.setdefault(ip, [])
    ATTEMPTS[ip] = [t for t in ATTEMPTS[ip] if now - t < window]

    if len(ATTEMPTS[ip]) >= limit:
        return False

    ATTEMPTS[ip].append(now)
    return True
