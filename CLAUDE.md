# Self-healing rules for process_contacts.py

- If `python3 process_contacts.py` fails with the same error 3 times in a row, stop retrying.
- Diagnose what changed first (`git log` / `git diff` on `contacts.csv` and the script).
- Fix the root cause, re-run to confirm, then append an entry to `fix_log.txt`.
- Known cause: the CSV header for the company column may be renamed (e.g. `company` -> `organization`).
  The script must read column names tolerantly instead of assuming one fixed header.
- Never fix by blindly retrying or by silently editing the data to match the code without logging it.
