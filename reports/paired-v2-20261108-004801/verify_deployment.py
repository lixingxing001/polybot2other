"""Read-only deployment audit. No settings writes or economic API calls."""
from decimal import Decimal
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import sqlite3
import time
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
REPORT = Path(__file__).resolve().parent
BASE = 'http://127.0.0.1:8791'


def read_api(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as response:
        return json.load(response)


def redacted(value):
    if isinstance(value, dict):
        return {k: redacted(v) for k, v in value.items() if k != 'control_token'}
    if isinstance(value, list):
        return [redacted(v) for v in value]
    return value


def connect(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('BEGIN')
    return connection


def table_hash(connection, table, ignore_verification_clocks=False):
    # Table names originate only from fixed local lists below.
    rows = [dict(r) for r in connection.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
    if ignore_verification_clocks:
        for row in rows:
            row.pop('verification_at', None)
            if row.get('verification_json'):
                evidence = json.loads(row['verification_json'])
                for field in ['checks_started_at', 'checks_completed_at']:
                    evidence.pop(field, None)
                row['verification_json'] = evidence
    payload = json.dumps(rows, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return {'count': len(rows), 'sha256': hashlib.sha256(payload.encode()).hexdigest()}


def conservation(path):
    with connect(path) as connection:
        wallet = dict(connection.execute('SELECT * FROM pair_wallet WHERE id=1').fetchone())
        groups = [json.loads(r[0]) for r in connection.execute('SELECT json FROM pair_groups')]
        pending = [json.loads(r[0]) for r in connection.execute("SELECT json FROM pair_fills WHERE status='MATCHED'")]
        cost = sum((Decimal(v) for g in groups for v in g['cost'].values()), Decimal(0))
        claims = sum((Decimal(g['claim']) for g in groups), Decimal(0))
        cash_due = sum((Decimal(f['cash']) for f in pending if f['action'] == 'SELL'), Decimal(0))
        pnl_due = sum((Decimal(f['cash']) - Decimal(f['basis']) for f in pending if f['action'] == 'SELL'), Decimal(0))
        residual = Decimal(wallet['cash']) + cost + claims + cash_due - Decimal(wallet['initial']) - Decimal(wallet['realized']) - pnl_due
        halt = connection.execute('SELECT reason FROM pair_v2_halt').fetchall()
        return {'wallet': wallet, 'cost': str(cost), 'claims': str(claims), 'pending_sell_cash': str(cash_due),
                'pending_sell_pnl': str(pnl_due), 'residual': str(residual), 'balanced': abs(residual) <= Decimal('1e-20'),
                'halt_reasons': [r[0] for r in halt]}


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []
    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        path = attributes.get('src') if tag == 'script' else attributes.get('href') if tag == 'link' else None
        if path and path.startswith('/static/'):
            self.sources.append(path)


def main():
    before = json.loads((REPORT / 'pre-deploy.json').read_text())
    v2, v1, live = read_api('/api/paired-v2'), read_api('/api/paired-paper'), read_api('/api/managed-live')
    (REPORT / 'running-v2-snapshot.json').write_text(json.dumps(redacted(v2), ensure_ascii=False, indent=2))
    protected = {}
    backup = ROOT / before['backup']
    pairs = [
        ('data/paired-paper-v1/paper.sqlite3', 'paired-v1.sqlite3', ['pair_wallet','pair_groups','pair_orders','pair_fills','pair_profit']),
        ('data/live-accounts/accounts.sqlite3', 'accounts.sqlite3', ['orders','fills','positions']),
        ('data/polybot2other-real-btc.sqlite3', 'polybot2other-real-btc.sqlite3', ['account','trades']),
        ('data/strategy-experiments/main_optimized.sqlite3', 'main_optimized.sqlite3', ['account','trades']),
        ('data/strategy-experiments/main_tp_sl.sqlite3', 'main_tp_sl.sqlite3', ['account','trades']),
        ('data/strategy-experiments/value_confirm_v1.sqlite3', 'value_confirm_v1.sqlite3', ['account','trades']),
    ]
    for current, old, tables in pairs:
        with connect(ROOT / current) as now_db, connect(backup / old) as old_db:
            protected[current] = {}
            for table in tables:
                old_hash, now_hash = table_hash(old_db, table), table_hash(now_db, table)
                protected[current][table] = {'before': old_hash, 'after': now_hash, 'unchanged': old_hash == now_hash}
                if table == 'orders':
                    protected[current][table]['unchanged_except_verification_clocks'] = (
                        table_hash(old_db, table, True) == table_hash(now_db, table, True))
    with urllib.request.urlopen(BASE + '/', timeout=20) as response:
        html, index_status = response.read().decode(), response.status
    parser = Assets(); parser.feed(html)
    assets = {}
    for path in parser.sources:
        if any(name in path for name in ['paired-paper', 'paired-v2', '/app.js']):
            with urllib.request.urlopen(BASE + path, timeout=20) as response:
                body = response.read()
                assets[path] = {'status': response.status, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
    suite = ET.parse(REPORT / 'final-tests.xml').getroot()[0].attrib
    keys = ['running','mode','version','enabled','error','rules_error','queue_size','dropped_events','last_tick','reason_code','filled_markets','completed_markets','cash','realized_pnl']
    report = {'at_ms': int(time.time()*1000), 'pid': (ROOT / 'data/polybot2other-macos-8791.pid').read_text().strip(),
              'v2': {k: v2.get(k) for k in keys}, 'stress': {k: v2['stress'].get(k) for k in keys if k not in ['running','error','rules_error','queue_size','dropped_events']},
              'exit_worker_running': v2['exit_worker_running'], 'exit_errors': v2['exit_errors'],
              'v1': {k: v1.get(k) for k in ['frozen','running','cash','realized_pnl','filled_markets','completed_markets']},
              'v1_entries_disabled': v1['settings']['enabled'] is False,
              'live_settings_unchanged': live['settings'] == before['live_settings'], 'live_settings': live['settings'],
              'protected_financial_tables': protected,
              'conservation': {name: conservation(ROOT / 'data/paired-paper-v2' / file) for name, file in [('TAPE','paper.sqlite3'),('STRESS','stress.sqlite3')]},
              'tests': {'pytest': 792, 'subtests': 177, 'junit': suite},
              'http': {'index_status': index_status, 'assets': assets},
              'visual_qa': {'status': 'NOT_COMPLETED', 'reason': 'CUA native pipe startup failed; browser host unavailable. No screenshot/visual approval claimed.'},
              'profit_validation': 'INSUFFICIENT: short forward sample; separate assumptions/turnover; no forecast of profitable or stable performance.'}
    report['critical_checks_passed'] = (
        v2['running'] and v2['mode'] == 'PAPER' and v2['error'] is None and v2['dropped_events'] == 0
        and report['exit_worker_running'] and report['live_settings_unchanged'] and report['v1_entries_disabled']
        and all(v['balanced'] and not v['halt_reasons'] for v in report['conservation'].values())
        and all(e['unchanged'] or e.get('unchanged_except_verification_clocks', False)
                for tables in protected.values() for e in tables.values()))
    (REPORT / 'deployment-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    with (REPORT / 'runtime-observation.jsonl').open('a') as out:
        out.write(json.dumps({k: report[k] for k in ['at_ms','pid','v2','stress','exit_worker_running','exit_errors','conservation']}, ensure_ascii=False) + '\n')
    print(json.dumps({'pid':report['pid'],'v2':report['v2'],'exit_worker':report['exit_worker_running'],
                      'conservation':{k:v['residual'] for k,v in report['conservation'].items()},
                      'live_settings_unchanged':report['live_settings_unchanged'],
                      'changed_protected_tables':[p+':'+t for p, tables in protected.items() for t, evidence in tables.items() if not evidence['unchanged']],
                      'http':report['http'],'visual_qa':report['visual_qa']}, ensure_ascii=False))
    assert report['critical_checks_passed'], 'Review deployment-verification.json for a failed critical check'


if __name__ == '__main__':
    main()
