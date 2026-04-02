from pathlib import Path

path = Path("/var/www/coin2win/app/main.py")
text = path.read_text()

start = text.find('@app.post("/deposit/create")')
end = text.find('@app.get("/deposit/', start)

if start == -1 or end == -1:
    raise SystemExit("No pude ubicar deposit_create")

deposit_block = text[start:end]

old = """        # KYC CHECK
        user = db.query(User).filter(User.id == user_id).one()
        if user.kyc_status != "verified":
            raise HTTPException(
                status_code=403,
                detail="KYC verification required before withdrawals are enabled."
            )

"""

if old not in deposit_block:
    raise SystemExit("No encontré el bloque KYC exacto dentro de deposit_create")

deposit_block = deposit_block.replace(old, "", 1)
text = text[:start] + deposit_block + text[end:]
path.write_text(text)
print("OK: KYC removido de deposit_create")
