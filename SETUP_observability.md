# SETUP — Observability Log Access (OpenObserve) di lark-agent-bridge

> Dibikin 2026-10-19 malam. Kode SELESAI & ketes (112 test pass). Ini langkah
> ngaktifin biar `/logs` & `/projects` jalan di chat. Semua di `D:\DXTR\lark-agent-bridge`.
> ⚠️ Creds OpenObserve diisi SENDIRI oleh Mayo (ga lewat Andre).

---

## Ringkas: 5 langkah
1. Bikin encryption key (simpen di luar repo)
2. Isi creds OpenObserve ke secret store terenkripsi
3. Bikin registry project (mapping project → org/stream)
4. Set env di `.env`
5. Restart bot → test `/projects` + `/logs`

---

## Langkah 1 — bikin encryption key
Key ini yang buka secret store. **Simpen di luar repo**, jangan commit.

```powershell
cd D:\DXTR\lark-agent-bridge
# bikin folder key di luar repo
mkdir $env:USERPROFILE\.lab -Force
# generate key -> tulis ke file (pakai venv repo)
.venv\Scripts\python.exe -c "from lark_agent_bridge.secrets import generate_key; open(r'$env:USERPROFILE\.lab\secret.key','w').write(generate_key())"
# cek keisi
type $env:USERPROFILE\.lab\secret.key
```
> Path final: `C:\Users\Dika\.lab\secret.key`. Jaga file ini — kalau hilang, secret store ga bisa dibuka (tinggal bikin ulang).

---

## Langkah 2 — isi creds OpenObserve ke secret store
Creds per-org (MTM-DEV/PRD/UAT punya user+token sendiri). Ganti nilai di bawah
dengan creds asli lu. **Jalanin di PowerShell, creds ga masuk chat.**

```powershell
cd D:\DXTR\lark-agent-bridge
.venv\Scripts\python.exe -c @"
from lark_agent_bridge.secrets import SecretStore
store = SecretStore('secrets.enc', key_file=r'C:\Users\Dika\.lab\secret.key')
store.set_many({
    # --- MTM-DEV ---
    'openobserve.MTM-DEV.user':  'EMAIL_OPENOBSERVE_LU',
    'openobserve.MTM-DEV.token': 'TOKEN_ATAU_PASSWORD_DEV',
    # --- MTM-PRD (kalau mau) ---
    # 'openobserve.MTM-PRD.user':  '...',
    # 'openobserve.MTM-PRD.token': '...',
    # --- MTM-UAT (kalau mau) ---
    # 'openobserve.MTM-UAT.user':  '...',
    # 'openobserve.MTM-UAT.token': '...',
})
store.save()
print('secrets.enc written:', store.has('openobserve.MTM-DEV.token'))
"@
```
> **Cara dapet token OpenObserve:** login OpenObserve → menu user/profile → biasanya
> ada "token"/API key, atau pakai email + password login. OpenObserve basic-auth =
> `(email, token_or_password)`. Kalau ragu, test langkah 5 — kalau 401, berarti creds salah.
>
> `secrets.enc` ke-gitignore (aman, ga ke-commit). Isinya terenkripsi AES-256-GCM.

---

## Langkah 3 — bikin registry project
File `observability_registry.json` di root repo (ke-gitignore). Mapping nama
project (yang lu ketik di chat) → lokasi log-nya.

```powershell
cd D:\DXTR\lark-agent-bridge
@'
{
  "projects": {
    "mt4api-dev": {
      "backend": "openobserve",
      "org": "MTM-DEV",
      "stream": "meta_python_mt4dev_connector",
      "default_window": "15m"
    }
  }
}
'@ | Out-File -Encoding utf8 observability_registry.json
```
> Nambah project lain = tambah entry. Contoh pola:
> - mtm dev: `"mtm-dev": {"backend":"openobserve","org":"MTM-DEV","stream":"<nama_stream_mtm>"}`
> - env prd: `"mt4api-prd": {"backend":"openobserve","org":"MTM-PRD","stream":"<stream_prd>"}`
>
> ⚠️ Nama `stream` harus **persis** sama kayak di OpenObserve (contoh dari link lu:
> `meta_python_mt4dev_connector`). Cek di UI OpenObserve bagian Streams.

---

## Langkah 4 — set env di `.env`
Tambahin baris ini ke `D:\DXTR\lark-agent-bridge\.env`:

```
ENABLE_OBSERVABILITY=1
OBSERVABILITY_REGISTRY=./observability_registry.json
OPENOBSERVE_BASE_URL=https://openobserve.surya-am.com
LAB_SECRETS_PATH=./secrets.enc
LAB_SECRET_KEY_FILE=C:\Users\Dika\.lab\secret.key
LOGS_DEFAULT_LIMIT=50
```

---

## Langkah 5 — restart bot & test
Restart proses `lark-agent-bridge` (sama kayak biasa lu restart bot). Lalu di chat Lark:

```
/projects
```
→ harus muncul `mt4api-dev — openobserve · org MTM-DEV · stream meta_python_mt4dev_connector`

```
/logs mt4api-dev
/logs mt4api-dev 1h
/logs mt4api-dev 15m error
```
→ harus balik log (atau "No log entries" kalau emang kosong).

---

## Troubleshooting
- **`/projects` bilang "not enabled"** → env `ENABLE_OBSERVABILITY=1` belum kebaca (cek .env, restart).
- **"OpenObserve rejected credentials (HTTP 401/403)"** → creds di langkah 2 salah. Ulang set, `store.save()`.
- **"unknown project"** → nama project ga ada di registry / typo. Cek `/projects`.
- **"No log entries in the last 15m"** → query jalan, emang ga ada log di window itu. Coba window lebih gede (`1h`, `6h`).
- **"request to OpenObserve failed"** → VPN/konektivitas ke openobserve.surya-am.com. Cek VPN.
- **Secret store error "no secret key configured"** → `LAB_SECRET_KEY_FILE` path salah / file ga ada.

## Catatan keamanan (penting)
- Default `/logs` **RAW** = hasil log langsung ke Lark, TIDAK lewat LLM. Aman buat data sensitif.
- `ALLOWED_OPEN_IDS` tetap kunci (cuma Mayo) — tool ini bisa baca log prod.
- Idealnya pakai **akun OpenObserve read-only**, bukan admin.
- Query scoped per-project (fail-closed) — ga ada "query semua index".

## Yang BELUM (buat sesi lanjut)
- **Grafana** (gateway log) — nunggu info: gateway log di Grafana itu via Loki datasource / panel metric / SQL? + 1 contoh dashboard/datasource uid. Begitu jelas, tinggal bikin `observability/grafana.py` (pola sama kayak openobserve.py).
- **Jira** connector (buat handover tiket) — fase berikutnya.
- Belum commit/push ke git (repo publik — pastiin `secrets.enc`/`*.key`/`registry.json` ke-gitignore; udah diset, tapi `git status` dulu sebelum commit).
