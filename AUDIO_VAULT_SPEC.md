# Audio Vault - specification for both versions

## IndexedDB store "audioVault"
- key: auto-increment id
- fields: blob (Blob), filename (string), dept, role, visibility, created (ISO), status ('pending'|'uploading'|'done'|'error'), jobId (string|null), errorMsg (string|null), retries (int)

## Flow:
1. Recording stops → blob saved to IndexedDB immediately with status='pending'
2. Upload starts → status='uploading'  
3. Upload success + poll done → status='done', remove from vault after 60s
4. Upload fails → status='error', errorMsg saved, blob stays in vault
5. On page load → check vault for pending/error items → show "unsent recordings" bar

## UI: Unsent recordings banner
- Shows count of unsent recordings above tabs
- Click to expand list with retry/download/delete buttons
- Each item shows: timestamp, size, status, retry button

## Web version: uses API_BASE
## Electron version: uses API variable
