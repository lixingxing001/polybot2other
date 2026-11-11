"""Replay the frozen MAIN signal code on its recorded inputs, without a broker.

This is the available, corrected MAIN implementation. It must not be labelled
as a reconstructed historical implementation or a full original account run.
"""
from __future__ import annotations
from dataclasses import asdict
import hashlib
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

from .contracts import digest

SIGNAL_FIELDS = ('max_entry_price', 'max_quote_age_ms', 'max_spread',
    'max_time_left_seconds', 'min_ask_size', 'min_confidence', 'min_edge',
    'min_price_distance_bps', 'min_time_left_seconds')


def capture_main_frame(frame, settings):
    """Derived input snapshot, never represented as an original remote frame."""
    return {'record_type': 'B1MarketInput', 'derived': True,
            'observed_at': int(frame.observed_at * 1000), 'input': asdict(frame),
            'signal_settings': {key: getattr(settings, key) for key in SIGNAL_FIELDS}}


class FrozenMainBaseline:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.manifest = json.loads((self.folder / 'manifest.json').read_text())
        asserted = self.manifest.get('manifest_sha256')
        if asserted != digest({k: v for k, v in self.manifest.items() if k != 'manifest_sha256'}):
            raise ValueError('BASELINE_MANIFEST_MISMATCH')
        for item in self.manifest['files']:
            path = (self.folder / item['path']).resolve()
            if not path.is_relative_to(self.folder):
                raise ValueError('BASELINE_SOURCE_PATH_INVALID')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != item['sha256']:
                raise ValueError('BASELINE_SOURCE_HASH_MISMATCH')
        settings_path = self.folder / 'baseline-settings.json'
        settings = json.loads(settings_path.read_text())
        settings_ref = self.manifest['settings']
        if hashlib.sha256(settings_path.read_bytes()).hexdigest() != settings_ref['sha256']:
            raise ValueError('BASELINE_SETTINGS_HASH_MISMATCH')
        values = settings.get('signal_settings', settings)
        if any(key not in values for key in SIGNAL_FIELDS):
            raise ValueError('BASELINE_SETTINGS_INCOMPLETE')
        self.signal_settings = {key: values[key] for key in SIGNAL_FIELDS}
        self.manifest_sha256 = asserted
        self.name = 'B1_CURRENT_FROZEN_UNIFIED'
        package_name = '_hengzhun_frozen_main_' + asserted[:20]
        if package_name not in sys.modules:
            package = ModuleType(package_name)
            package.__path__ = [str(self.folder / 'src/polybot2other')]
            sys.modules[package_name] = package
        self.models = importlib.import_module(package_name + '.models')
        self.shared = importlib.import_module(package_name + '.shared_market_inputs')
        self.signals = importlib.import_module(package_name + '.managed_live_signals')
        config = importlib.import_module(package_name + '.config')
        self.settings = config.Settings(**self.signal_settings)

    def evaluate(self, recorded, decision_at, *, fee_rate, fee_exponent):
        if recorded.get('record_type') != 'B1MarketInput' or recorded.get('derived') is not True:
            raise ValueError('BASELINE_INPUT_UNAVAILABLE')
        if type(decision_at) is not int or recorded['observed_at'] > decision_at:
            raise ValueError('BASELINE_INPUT_NOT_VISIBLE')
        if recorded.get('signal_settings') != self.signal_settings:
            raise ValueError('BASELINE_CONFIG_CHANGED')
        row = recorded['input']
        if row['market'] is None:
            return {'available': False, 'reason': 'BASELINE_MARKET_MISSING'}
        # Let the exact frozen adapter perform its original quality checks.
        frame = self.shared.MarketInputs(
            self.models.MarketRound(**row['market']), row['reference'], row['price'], row['quotes'],
            decision_at / 1000, row['snapshot_id'], row['error'])
        frame_error = self.shared.reference_error(frame.market, frame.reference, decision_at / 1000)
        if frame_error:
            return {'available': True, 'side': 'NO_TRADE', 'reason': frame_error,
                    'baseline_manifest_sha256': self.manifest_sha256}
        context = SimpleNamespace(settings=self.settings)
        # MAIN has no ValueConfirm/optimized state dependency. Risk and execution
        # are deliberately outside this exact signal adapter.
        signal, limit = self.signals.LiveSignals.evaluate(context, 'MAIN',
            {'stake_dollars': 5.0, 'max_open_trades': 1, 'max_daily_loss': 10.0},
            frame.market, frame.reference, frame.quotes, frame.observed_at,
            {'fee_rate': fee_rate, 'fee_exponent': fee_exponent}, market_input=frame)
        return {'available': True, **asdict(signal), 'limit_price': limit,
                'baseline_manifest_sha256': self.manifest_sha256,
                'signal_code': 'FROZEN_ACTUAL_MAIN', 'account_variant': self.name}
