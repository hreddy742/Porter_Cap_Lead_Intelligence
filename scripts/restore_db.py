import subprocess
from pathlib import Path

BACKUP_DIR = Path("backups")
backups = sorted(BACKUP_DIR.glob("porter_leads_*.sql"), reverse=True)

if not backups:
    print("No backups found in backups/ directory")
    exit(1)

print("Available backups:")
for i, f in enumerate(backups):
    size = f.stat().st_size / 1024 / 1024
    print(f"  [{i}] {f.name} ({size:.1f} MB)")

choice = input("Which backup to restore? (enter number): ")
backup_file = backups[int(choice)]

print(f"Restoring from {backup_file.name}...")
confirm = input("This will OVERWRITE the current database. Type YES to confirm: ")

if confirm != "YES":
    print("Cancelled.")
    exit(0)

subprocess.run([
    "docker", "exec", "-i", "porter-leads-db-1",
    "psql", "-U", "postgres", "-c",
    "DROP DATABASE IF EXISTS porter_leads; CREATE DATABASE porter_leads OWNER porter;"
])

result = subprocess.run(
    ["docker", "exec", "-i", "porter-leads-db-1",
     "psql", "-U", "porter", "porter_leads"],
    input=backup_file.read_text(),
    capture_output=True,
    text=True
)

if result.returncode == 0:
    print(f"Restored successfully from {backup_file.name}")
else:
    print(f"Restore FAILED: {result.stderr}")
