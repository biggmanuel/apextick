"""Runs every test_*.py in this folder (each uses its own temporary database) and prints a summary."""
import glob, os, subprocess, sys

failed = []
for f in sorted(glob.glob("test_*.py")):
    r = subprocess.run([sys.executable, f], capture_output=True, text=True)
    lines = (r.stdout + r.stderr).splitlines()
    passes = sum(1 for l in lines if l.startswith("PASS "))
    fails = [l for l in lines if l.startswith("FAIL ")]
    ok = r.returncode == 0 and not fails
    print(f"{'OK  ' if ok else 'FAIL'} {f:<28} {passes} checks passed")
    for l in fails:
        print("     ", l)
    if not ok:
        failed.append(f)
        if r.returncode != 0 and not fails:
            print("      (crashed)", "\n      ".join(lines[-6:]))

print("\nALL GOOD" if not failed else f"\nFAILED: {failed}")
sys.exit(1 if failed else 0)