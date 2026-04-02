from pathlib import Path
import re

path = Path("/var/www/coin2win/app/main.py")
text = path.read_text()

start = text.find('@app.post("/deposit/create")')
end = text.find('@app.post("/withdraw/create")')

if start == -1 or end == -1 or end <= start:
    raise SystemExit("No pude encontrar deposit_create o withdraw_create")

deposit_block = text[start:end]

deposit_block_clean = re.sub(
    r'\n\s*# KYC CHECK\s*'
    r'\n\s*user = db\.query\(User\)\.filter\(User\.id == user_id\)\.one\(\)\s*'
    r'\n\s*if user\.kyc_status != "verified":\s*'
    r'\n\s*raise HTTPException\(\s*'
    r'\n\s*status_code=403,\s*'
    r'\n\s*detail="KYC verification required before withdrawals are enabled\."\s*'
    r'\n\s*\)\s*',
    '\n',
    deposit_block,
    flags=re.S
)

if deposit_block == deposit_block_clean:
    print("No encontré KYC check dentro de deposit_create.")
else:
    text = text[:start] + deposit_block_clean + text[end:]
    path.write_text(text)
    print("OK: KYC removido de deposit_create")
