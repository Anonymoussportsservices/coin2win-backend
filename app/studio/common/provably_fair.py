import hmac
import hashlib

def hmac_sha256_hex(server_seed: str, message: str) -> str:
    return hmac.new(
        server_seed.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()

def hex_to_int(hex_str: str) -> int:
    return int(hex_str, 16)

def float_from_hex(hex_str: str) -> float:
    n = hex_to_int(hex_str[:13])
    return n / float(2**52)
