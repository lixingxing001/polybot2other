"""原始设计附件的结构校验和固定语义哈希；未知事实不能自动变为已核验。"""
from copy import deepcopy
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path

from jsonschema import Draft202012Validator

SPEC_DIR = Path(__file__).with_name('spec')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


@lru_cache(maxsize=64)
def validator(schema_name='event_contracts.schema.json', definition=None):
    schema = json.loads((SPEC_DIR / schema_name).read_text())
    if definition:
        schema = {'$schema':schema['$schema'], '$defs':schema['$defs'], '$ref':'#/$defs/'+definition}
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_record(record, schema_name='event_contracts.schema.json', definition=None):
    validator(schema_name, definition).validate(record)
    if definition == 'MarketContractSnapshot' or record.get('record_type') == 'MarketContractSnapshot':
        if record['window_close_at'] - record['window_open_at'] != 300000:
            raise ValueError('CONTRACT_WINDOW_INVALID')
        if record['up_asset_id'] and record['up_asset_id'] == record['down_asset_id']:
            raise ValueError('CONTRACT_ASSET_CONFLICT')
        if record['contract_sha256'] != contract_hash(record):
            raise ValueError('CONTRACT_HASH_MISMATCH')
    return record


def contract_hash(contract):
    """审计请求时间不参与合同版本；固定白名单只覆盖影响经济结果的语义。"""
    result = {k:deepcopy(contract[k]) for k in ('market','slug','market_protocol_version','up_asset_id',
        'down_asset_id','collateral_asset_id','collateral_decimals','window_open_at','window_close_at',
        'window_duration_seconds','rules','trading','versions')}
    result['baseline'] = {k:contract['baseline'][k] for k in ('value','source_at','status',
        'resolution_feed_id','boundary_rule_id','verification_method')}
    # accepting_orders / resync are live gates, not a new immutable price contract.
    for field in ('accepting_orders','book_state','delay_evidence'):
        result['trading'].pop(field, None)
    result['trading']['fee_contract'].pop('evidence', None)
    return digest(result)


class RealVenue:
    """没有 signer 或钱包连接，所有资金动作都硬失败。"""
    def __init__(self, *args, **kwargs):
        raise RuntimeError('LIVE_EXECUTION_DISABLED')


def require_verified(contract):
    validate_record(contract, definition='MarketContractSnapshot')
    if contract['verification_status'] != 'VERIFIED':
        raise ValueError('CONTRACT_UNVERIFIED')
    baseline=contract['baseline'];rules=contract['rules']
    if baseline['source_at']!=contract['window_open_at']:
        raise ValueError('BASELINE_BOUNDARY_MISMATCH')
    if baseline['resolution_feed_id']!=rules['resolution_feed_id']:
        raise ValueError('BASELINE_FEED_MISMATCH')
    if not baseline['source_at']<=baseline['received_at']<=baseline['verified_at']<=contract['captured_at']:
        raise ValueError('BASELINE_EVIDENCE_TIME_INVALID')
    if not contract['trading']['accepting_orders'] or contract['trading']['book_state'] != 'READY':
        raise ValueError('MARKET_NOT_ACCEPTING')

