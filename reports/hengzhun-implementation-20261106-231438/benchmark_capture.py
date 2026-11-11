"""Read actual archived public frames; benchmark only disposable journals."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import time

import polybot2other.hengzhun.data as data
from polybot2other.hengzhun.contracts import validate_record


def run(rows,*,old_validation=False):
    original=data.validate_constructed_book
    if old_validation:data.validate_constructed_book=lambda view:validate_record(view,definition='BookView')
    try:
        with tempfile.TemporaryDirectory() as folder:
            journal=data.EventJournal(Path(folder)/'disposable.sqlite3');books=data.AsOfBooks()
            started=time.perf_counter();milestones=[];errors=0
            for first in range(0,len(rows),100):
                with journal.transaction():
                    for event in rows[first:first+100]:
                        journal.append(source_kind='ORDER_BOOK',source_id=event['source_id'],stream_id=event['stream_id'],
                            payload=event['payload'],received_at=event['received_at'],source_at=event['source_at'],market=event['market'])
                        try:books.ingest(event['payload'],event['source_at'],event['received_at'],event['event_id'])
                        except ValueError:errors+=1
                milestones.append({'events':min(first+100,len(rows)),'elapsed_seconds':time.perf_counter()-started})
            duration=time.perf_counter()-started
            rows_count=journal.counts()['raw_events'];journal.conn.close()
        return {'events':len(rows),'archived_events':rows_count,'book_rejections':errors,'seconds':duration,
            'events_per_second':len(rows)/duration,'batch_size':100,'synchronous':'FULL',
            'validation':'ORIGINAL_RECURSIVE_SCHEMA' if old_validation else 'EXACT_FACTORED_SCHEMA_CACHED_LEVELS',
            'milestones':milestones}
    finally:data.validate_constructed_book=original


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('source',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    with sqlite3.connect(args.source.resolve().as_uri()+'?mode=ro',uri=True) as connection:
        rows=[json.loads(row[0]) for row in connection.execute("SELECT json FROM raw_events WHERE kind='ORDER_BOOK' ORDER BY seq LIMIT 3000")]
    if len(rows)<2000:raise RuntimeError('Need at least 2000 actual captured book events')
    # Both runs use batching, so the comparison isolates the repeated depth
    # validation cost. The warm segment includes at least 2000 real events.
    before=run(rows[:300],old_validation=True);after=run(rows)
    warm=after['milestones'][9]
    after['steady_events_per_second']=(after['events']-warm['events'])/(after['seconds']-warm['elapsed_seconds'])
    report={'source':str(args.source),'read_only_source':True,'before':before,'after':after,
        'level_cache':data._validate_book_level.cache_info()._asdict(),
        'meets_1000_events_per_second_steady':after['steady_events_per_second']>=1000}
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'before_events_per_second':before['events_per_second'],'after_events_per_second':after['events_per_second'],
        'steady_events_per_second':after['steady_events_per_second'],'archived_events':after['archived_events'],
        'book_rejections':after['book_rejections'],'report':str(args.output)}))
