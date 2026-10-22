"""Reproduce audit findings on isolated temporary files. Run from the repository root.
No broker, network, credentials, or real accounts are used. Exit 0 means the findings
were reproduced, NOT that the product passed a safety test.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path.cwd()))
from lab import backtest, data, experiments
from lab.agents.execution import ExecutionAgent
from lab.agents.signal_agent import SignalAgent
from lab.cli import _period, _with_target_vol, build_parser, cmd_health
from lab.config import RiskSettings, load_config
from lab.halt import read_halt
from lab.research.sandbox import static_check
from lab.safety import read_kill_switch
from lab.signals.base import Signal
from lab.signals.registry import SignalSpec, load_class, resolve
from lab.sim.broker import SimBroker
from lab.sim.harness import LiveHarness

ROOT = Path.cwd()


def local_cfg(tmp):
    cfg = load_config(ROOT / 'lab.toml')
    return replace(cfg, root=tmp, results_dir=tmp/'results', research_dir=tmp/'research',
                   data_dir=tmp/'data', universe={'GOOG': 'XNAS'})


class Short(Signal):
    name = 'audit_short'
    def target(self, w):
        return -1.0


def main():
    evidence = {}
    with tempfile.TemporaryDirectory(prefix='trading-audit-') as raw:
        tmp = Path(raw)
        cfg = local_cfg(tmp)
        # F1: run the real LiveNode with an independent broker cash balance.
        h = LiveHarness(cfg, [SignalSpec('buy_and_hold')], {'GOOG': data.sample_goog()},
                        tmp/'broker-run', first_day=556, broker_cash=1000)
        session = h.session()
        kinds = [e['kind'] for e in h.journal() if e.get('type') == 'RiskEvent']
        diff = h.ledger()['cash'] - h.broker().cash
        assert session.error is None and session.fills and abs(diff) > 10000
        assert 'reconciliation_mismatch' not in kinds
        evidence['F1'] = {'ledger_minus_broker_cash': diff, 'fills': len(session.fills), 'halt': read_halt(h.runtime/'HALT')}

        # F2: the live historical-bar intake admits both old and future sessions.
        now = datetime(2026, 10, 22, 17, tzinfo=UTC)
        stamps = [int(pd.Timestamp('2026-10-01T20:00Z').value), int(pd.Timestamp('2026-11-01T20:00Z').value)]
        admitted = []
        dummy = SimpleNamespace(config=SimpleNamespace(session_close='16:00'), clock=None, last_ts=-1,
                                _process=lambda b, publish: admitted.append((b.ts_event, publish)))
        with patch('lab.agents.signal_agent.now_ny', return_value=now):
            SignalAgent.on_historical_bars(dummy, [SimpleNamespace(ts_event=s) for s in stamps])
        held = SimpleNamespace(config=SimpleNamespace(live=True, borrow_book=None), held={'GOOG': 10},
                               prices={'GOOG': 100}, price_stamps={'GOOG': stamps[0]}, price_ts=stamps[0])
        assert admitted[-1] == (stamps[-1], True) and ExecutionAgent._unsafe_held_marks(held) == []
        evidence['F2'] = {'historical_stamps_admitted': admitted, 'old_held_mark_rejected': False}

        # F3: a short squeeze through zero equity leaves the position unclosed.
        frame = pd.DataFrame({'open':100., 'high':101., 'low':99., 'close':100., 'volume':1e7},
                             index=pd.bdate_range('2021-01-04', periods=20))
        frame.loc[frame.index[8]:, ['open','high','low','close']] *= 3
        risk = RiskSettings(1, 1, .99, max_short_gross=1, min_net=-1, max_net=1)
        result = backtest.run(replace(cfg, risk=risk), [backtest.Sleeve(Short(), 'GOOG', 1)], {'GOOG':frame},
                              frame.index[1].date(), None)
        pos = sum(f.qty if f.side == 'BUY' else -f.qty for f in result.fills)
        assert result.equity.min() < 0 and pos < 0 and not any(f.side == 'BUY' for f in result.fills)
        evidence['F3'] = {'minimum_equity': float(result.equity.min()), 'unclosed_short':pos,
                           'fill_sides':[f.side for f in result.fills]}

        # F4: an allowed numpy method writes a harmless marker while loading the class.
        marker = tmp/'only-audit-marker.bin'
        source = f'''import numpy as np
from lab.signals.base import Signal
class Probe(Signal):
    name = "probe"
    description = "audit only"
    lookback = 1
    defaults = {{}}
    marker = np.array([7]).tofile({str(marker)!r})
    def target(self, w):
        return 0.0
'''
        candidate = tmp/'signal.py'
        candidate.write_text(source)
        check = static_check(source)
        assert check.ok
        load_class(candidate)
        assert marker.exists()
        evidence['F4'] = {'static_check_passed':check.ok, 'parent_process_file_write':marker.exists()}

        # F5: promoted status is not bound to the validated source or parameters.
        hdir = cfg.research_dir/'hypotheses'/'mutable'
        hdir.mkdir(parents=True)
        hdir.joinpath('status.json').write_text(json.dumps({'status':'promoted'}))
        plain = '''from lab.signals.base import Signal
class Mutable(Signal):
    name = "mutable"
    description = "audit"
    lookback = 1
    defaults = {}
    def target(self, w):
        return VALUE
'''
        hdir.joinpath('signal.py').write_text(plain.replace('VALUE','0.1'))
        first = resolve('mutable', cfg.research_dir, require_tradable=True).make().target(None)
        # Different length avoids a same-size timestamp-based .pyc cache masking the change.
        hdir.joinpath('signal.py').write_text(plain.replace('VALUE','0.999'))
        changed = resolve('mutable', cfg.research_dir, require_tradable=True).make().target(None)
        assert first == .1 and changed == .999
        evidence['F5'] = {'previous_approved_target':first, 'mutated_source_accepted_target':changed}

        # F6: _null_worker gives the sampler full frames before applying the train period.
        original = data.gbm(400, seed=7)
        revised = original.copy()
        revised.iloc[200:, :4] *= np.exp(np.arange(200)*.02)[:,None]
        assert original.iloc[:200].equals(revised.iloc[:200])
        a = data.block_bootstrap_panel({'GOOG':original}, seed=7)['GOOG'].iloc[:200]
        b = data.block_bootstrap_panel({'GOOG':revised}, seed=7)['GOOG'].iloc[:200]
        assert not a.equals(b)
        evidence['F6'] = {'raw_train_unchanged':True, 'resampled_train_changed_by_future_rows':True}

        # F7: explicit dates relabel the holdout as custom and bypass the CLI guards.
        args = build_parser().parse_args(['backtest','--start','2023-01-01','--end','2023-12-31'])
        period = _period(cfg,args)
        assert period.name == 'custom' and period.start >= cfg.period('holdout').start
        evidence['F7'] = {'period_label':period.name, 'start':str(period.start), 'guard_checks_only_period_label':True}

        # F8: the trial ledger collapses two distinct risk configurations into one trial.
        spec = SignalSpec('sma_trend')
        active = pd.Series([.01,-.01,.02,.001])
        experiments.register_trial(cfg, 'audit', spec, 'first', active)
        experiments.register_trial(replace(cfg,risk=replace(cfg.risk,max_gross=.5)), 'audit',spec,'second',-active)
        trials = experiments.trial_sharpes(cfg,'audit')
        assert len(trials)==1
        evidence['F8'] = {'evaluated_risk_configurations':2, 'counted_trials':len(trials)}

        # F9: the optional CLI override bypasses the selected profile's volatility cap.
        path = tmp/'profile.toml'
        path.write_text((ROOT/'lab.toml').read_text().replace('profile = "custom"','profile = "calm"'))
        calm = load_config(path)
        overridden = _with_target_vol(calm,SimpleNamespace(target_vol=1.0))
        assert calm.portfolio.target_vol == .06 and overridden.portfolio.target_vol == 1.0
        evidence['F9']={'calm_target_vol':calm.portfolio.target_vol,'cli_override':overridden.portfolio.target_vol}

        # F10: infinity passes price validation; incomplete OHLC rows silently disappear.
        bad = original.iloc[:3].copy()
        bad.iloc[1,:4] = np.inf
        accepted = data.validate(bad,'AUDIT')
        assert np.isinf(accepted.iloc[1,:4]).all()
        missing = original.iloc[:3].copy()
        missing.iloc[1,0] = np.nan
        dropped = data.validate(missing,'AUDIT')
        assert len(dropped)==2
        evidence['F10']={'infinite_ohlc_accepted':True,'incomplete_rows_silently_dropped':1}

        # F11: valid JSON with the wrong shape crashes the supposed fail-closed readers.
        errors={}
        for name, reader in [('KILL',read_kill_switch),('HALT',read_halt)]:
            p=tmp/name
            p.write_text('[]')
            try:
                reader(p)
            except Exception as exc:
                errors[name]=type(exc).__name__
        assert errors=={'KILL':'AttributeError','HALT':'AttributeError'}
        evidence['F11']=errors

        # F12: health checks say a cleanly stopped node is healthy while timestamp is recent.
        hb=h.runtime/'heartbeat.json'
        hb.write_text(json.dumps({'at':datetime.now(UTC).isoformat(),'running':False}))
        with contextlib.redirect_stdout(io.StringIO()):
            status=cmd_health(cfg,SimpleNamespace(max_age=180))
        assert status==0
        evidence['F12']={'running':False,'health_exit_code':status}

        # F13: the smoke order's "cannot fill" claim is false for a gap below its limit.
        broker=SimBroker(tmp/'smoke.json',cash=1000,slippage=False)
        broker.submit(client_order_id='AUDIT',ticker='X',side='BUY',qty=1,order_type='LIMIT',tif='DAY',limit=50)
        fills,_=broker.open_session({'X':40},ts=1)
        assert len(fills)==1
        evidence['F13']={'last_close':100,'limit':50,'gap_open':40,'filled':len(fills)}

        # F14: removing the trailer bypasses the collaboration author's check.
        spec=importlib.util.spec_from_file_location('collab_audit',ROOT/'tools/collab.py')
        tool=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        repo=tmp/'unsigned'
        repo.mkdir()
        def git(*args):
            return subprocess.run(['git',*args],cwd=repo,check=True,capture_output=True,text=True).stdout.strip()
        git('init','-q','-b','chatgpt/audit')
        git('config','user.name','Audit')
        git('config','user.email','audit@example.invalid')
        git('commit','--allow-empty','-qm','baseline')
        base=git('rev-parse','HEAD')
        git('commit','--allow-empty','-qm','unsigned change')
        tool.ROOT=repo
        with contextlib.redirect_stdout(io.StringIO()):
            code=tool.main(['check','--base',base])
        assert code==0
        evidence['F14']={'unsigned_commit_check_exit_code':code}
    print(json.dumps({'tested_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
                      'findings_reproduced':evidence},indent=2))

if __name__=='__main__':
    main()
