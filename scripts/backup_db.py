import subprocess
import os
from datetime import datetime, timedelta
from pathlib import Path

BACKUP_DIR = Path("backups")
BACKUP_DIR.mkdir(exist_ok=True)

today = datetime.now().strftime("%Y-%m-%d")
backup_file = BACKUP_DIR / f"porter_leads_{today}.sql"

result = subprocess.run([
    "docker", "exec", "porter-leads-db-1",
    "pg_dump", "-U", "porter", "porter_leads"
], capture_output=True)

if result.returncode == 0:
    backup_file.write_bytes(result.stdout)
    print(f"Backup saved: {backup_file}")
    print(f"Size: {backup_file.stat().st_size / 1024 / 1024:.1f} MB")
else:
    error = result.stderr.decode('utf-8', errors='replace')
    print(f"Backup FAILED: {error}")
    exit(1)

cutoff = datetime.now() - timedelta(days=7)
for f in BACKUP_DIR.glob("porter_leads_*.sql"):
    try:
        date_str = f.stem.replace("porter_leads_", "")
        file_date = datetime.strptime(date_str, "%Y-%m-%d")
        if file_date < cutoff:
            f.unlink()
            print(f"Deleted old backup: {f.name}")
    except Exception:
        pass
