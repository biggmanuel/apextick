#!/usr/bin/env python3
"""
Reads a saved dashboard page (.mhtml from Chrome "Save as") and prints it as plain text lines.
The user shares dashboard snapshots this way. Chrome strips the page's JavaScript when saving, so only the
rendered data is available (KPIs, positions, exit tables, logs).

Usage:  python3 tools/parse_snapshot.py <file.mhtml> [max_lines_before_logs] [max_log_lines]
        from tools.parse_snapshot import load      # -> list of non-empty text lines
Sections to look for in the list: 'ACTIVE DEPLOYMENT LOG (75/25 SNIPER)', 'FUTURES EXITS', 'SPOT EXITS', 'SYSTEM LOGS'.
Exit tables are 5 cells per row (older page) or 6 for futures (newer page with the Type column).
"""
import email, re, html, glob, sys
from email import policy
def load(f):
    raw=open(f,encoding='utf-8',errors='replace').read()
    try:
        m = email.message_from_string(raw, policy=policy.default)
        t=[p.get_content() for p in m.walk() if p.get_content_type()=='text/html'][0]
    except Exception:
        t=raw
    t=re.sub(r'<style.*?</style>','',t,flags=re.S); t=re.sub(r'<script.*?</script>','',t,flags=re.S)
    t=html.unescape(re.sub(r'<[^>]+>','\n',t))
    return [l.strip() for l in t.split('\n') if l.strip()]
if __name__=='__main__':
    L=load(sys.argv[1])
    i=L.index('SYSTEM LOGS') if 'SYSTEM LOGS' in L else len(L)
    print('\n'.join(L[:i][:int(sys.argv[2])]))
    print('---LOGS---'); print('\n'.join(L[i:i+int(sys.argv[3])]))
