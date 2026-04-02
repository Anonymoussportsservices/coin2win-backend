from pathlib import Path

current = Path("/var/www/coin2win/app/main.py")
backup = Path("/var/www/coin2win/app/main.py.bak_kyc_me_2026-03-25_041851")

cur = current.read_text()
bak = backup.read_text()

start_marker = '@app.post("/deposit/create")'
end_marker = '@app.get("/deposit/'

cur_start = cur.find(start_marker)
cur_end = cur.find(end_marker, cur_start)

bak_start = bak.find(start_marker)
bak_end = bak.find(end_marker, bak_start)

if cur_start == -1 or cur_end == -1:
    raise SystemExit("No pude encontrar deposit_create en el archivo actual")

if bak_start == -1 or bak_end == -1:
    raise SystemExit("No pude encontrar deposit_create en el backup")

good_block = bak[bak_start:bak_end]
new_text = cur[:cur_start] + good_block + cur[cur_end:]

current.write_text(new_text)
print("OK: deposit_create restaurado desde backup")
