use std::str::FromStr;
use trading_core_types::{FixedPrice, FixedQty, FixedUsd, SCALE};
use trading_orderbook::{FlatOrderBook, LevelDelta, PriceLevel, Side};
use trading_signals::PairSymbol;
use trading_signals::{
    AbsorptionEngine, HawkesCascadeEngine, HawkesEvent, KalmanLeadLagEngine, MicroPriceEngine,
    MlOfiEngine, SignalError, VpinToxicityEngine,
};

fn p(s: &str) -> FixedPrice {
    FixedPrice::from_str(s).unwrap()
}
fn q(s: &str) -> FixedQty {
    FixedQty::from_str(s).unwrap()
}
fn usd(s: &str) -> FixedUsd {
    FixedUsd::from_str(s).unwrap()
}

fn synthetic_book() -> FlatOrderBook {
    let mut b = FlatOrderBook::new(64).unwrap();
    let bids: Vec<_> = (0..10)
        .map(|i| PriceLevel {
            price: FixedPrice((10_000 - i) * 1_000_000),
            qty: q("2"),
        })
        .collect();
    let asks: Vec<_> = (0..10)
        .map(|i| PriceLevel {
            price: FixedPrice((10_100 + i) * 1_000_000),
            qty: q("1"),
        })
        .collect();
    b.load_snapshot(&bids, &asks).unwrap();
    b
}

#[test]
fn ofi_is_unavailable_before_two_valid_books() {
    let b = synthetic_book();
    let mut engine = MlOfiEngine::new();
    assert!(engine.update(1, &b).unwrap().is_none());
    assert!(engine.update(1, &b).unwrap().is_some());
}

#[test]
fn ofi_positive_for_bid_replenishment() {
    let mut b = synthetic_book();
    let mut e = MlOfiEngine::new();
    e.update(1, &b).unwrap();
    b.apply_batch(&[LevelDelta::new(Side::Bid, p("100"), q("3")).unwrap()])
        .unwrap();
    let value = e.update(100_000_001, &b).unwrap().unwrap();
    assert!(value.ofi_base_units > 0.0);
    assert!(value.z_score.is_finite());
}

#[test]
fn ofi_rejects_time_reversal_and_reset_clears_warmup() {
    let b = synthetic_book();
    let mut e = MlOfiEngine::new();
    e.update(200, &b).unwrap();
    assert_eq!(e.update(199, &b), Err(SignalError::NonMonotonicTime));
    e.reset();
    assert!(e.update(199, &b).unwrap().is_none());
}

#[test]
fn microprice_and_vacuum_are_bounded_and_quote_sensitive() {
    let mut b = synthetic_book();
    let mut m = MicroPriceEngine::new();
    let first = m.update(1, &b).unwrap().unwrap();
    assert!((first.downward_vacuum_ratio - 1.0).abs() < 1e-12);
    assert!(first.microprice > first.mid);
    let changed_bids: Vec<_> = b
        .bids()
        .iter()
        .copied()
        .map(|mut x| {
            x.qty = q("1");
            x
        })
        .collect();
    let asks = b.asks().to_vec();
    b.load_snapshot(&changed_bids, &asks).unwrap();
    let second = m.update(1_800_000_000_001, &b).unwrap().unwrap();
    assert!(second.downward_vacuum_ratio < 1.0);
    assert!(second.imbalance < first.imbalance);
}

#[test]
fn microprice_rejects_unprovable_truncated_depth() {
    let mut b = FlatOrderBook::new(10).unwrap();
    let x = synthetic_book();
    b.load_snapshot(x.bids(), x.asks()).unwrap();
    let mut m = MicroPriceEngine::new();
    assert_eq!(m.update(1, &b), Err(SignalError::TruncatedBook));
}

#[test]
fn vpin_requires_fifty_completed_buckets() {
    let mut v = VpinToxicityEngine::new(usd("10")).unwrap();
    for _ in 0..49 {
        assert!(v.observe(usd("10"), true).unwrap().is_none());
    }
    let s = v.observe(usd("10"), true).unwrap().unwrap();
    assert_eq!(s.completed_buckets, 50);
    assert!((s.toxicity - 1.0).abs() < 1e-12);
    assert!((s.signed_sell_toxicity - 1.0).abs() < 1e-12);
}

#[test]
fn vpin_partial_splits_and_signed_direction() {
    let mut v = VpinToxicityEngine::new(usd("10")).unwrap();
    v.observe(usd("3"), true).unwrap();
    v.observe(usd("7"), false).unwrap();
    assert_eq!(v.completed_buckets(), 1);
    for _ in 0..49 {
        v.observe(usd("10"), false).unwrap();
    }
    let s = v.snapshot().unwrap();
    assert!(s.toxicity < 1.0);
    assert!(s.signed_sell_toxicity < 0.0);
}

#[test]
fn vpin_large_trade_bulk_processing_is_bounded_and_correct() {
    let mut v = VpinToxicityEngine::new(usd("1")).unwrap();
    v.observe(FixedUsd(i64::MAX), false).unwrap();
    let s = v.snapshot().unwrap();
    assert_eq!(s.toxicity, 1.0);
    assert_eq!(s.signed_sell_toxicity, -1.0);
}

#[test]
fn vpin_rejects_zero_or_negative_volume() {
    assert!(VpinToxicityEngine::new(usd("0")).is_err());
    let mut v = VpinToxicityEngine::new(usd("1")).unwrap();
    assert_eq!(v.observe(usd("0"), true), Err(SignalError::InvalidQuantity));
}

#[test]
fn hawkes_separates_upward_downward_flows() {
    let mut h = HawkesCascadeEngine::new(1.0).unwrap();
    let d = h
        .observe(1, HawkesEvent::AggressiveSell, usd("10"))
        .unwrap();
    assert!(d.isr_down > 1.0);
    assert_eq!(d.isr_up, 1.0);
    let u = h.observe(1, HawkesEvent::AggressiveBuy, usd("10")).unwrap();
    assert!(u.isr_up > 1.0);
    assert!(u.isr_down > 1.0);
    assert!((u.fixed_kernel_spectral_radius - 2.6).abs() < 1e-12);
}

#[test]
fn hawkes_forced_liquidation_excites_fast_and_slow_channels() {
    let mut h = HawkesCascadeEngine::new(1.0).unwrap();
    let x = h
        .observe(1, HawkesEvent::ForcedLongLiquidation, usd("10"))
        .unwrap();
    assert!(x.effective_branching_proxy > 0.0);
    assert!(x.isr_down > 7.0);
    let later = h.advance(10_000_000_001).unwrap();
    assert!(later.isr_down < x.isr_down);
    assert!(later.isr_down >= 1.0);
}

#[test]
fn hawkes_rejects_backward_clock_and_invalid_baseline() {
    assert!(HawkesCascadeEngine::new(0.0).is_err());
    let mut h = HawkesCascadeEngine::new(1.0).unwrap();
    h.advance(200).unwrap();
    assert_eq!(h.advance(199), Err(SignalError::NonMonotonicTime));
}

#[test]
fn absorption_at_stationary_bid_is_high_but_not_proof_of_iceberg() {
    let mut a = AbsorptionEngine::new();
    let s = a.update(1, p("100"), q("5"), q("1")).unwrap();
    assert!(s.absorption_ratio > 4.0);
    assert_eq!(s.downward_displacement_bps, 0.0);
}

#[test]
fn absorption_price_fall_reduces_ratio() {
    let mut a = AbsorptionEngine::new();
    let first = a.update(1, p("100"), q("5"), q("1")).unwrap();
    let next = a.update(100_000_001, p("99"), q("0"), q("1")).unwrap();
    assert!(next.absorption_ratio < first.absorption_ratio);
    assert!(next.downward_displacement_bps > 0.0);
}

#[test]
fn absorption_rejects_bad_normalizer_without_state_update() {
    let mut a = AbsorptionEngine::new();
    assert!(a.update(1, p("100"), q("1"), q("0")).is_err());
    assert!(a.update(1, p("100"), q("1"), q("1")).is_ok());
}

#[test]
fn kalman_accepts_only_overlapping_real_observations() {
    let mut k = KalmanLeadLagEngine::new(1.0, 1.0, 1e-9, 1e-7).unwrap();
    assert!(
        k.observe(PairSymbol::Leader, 1, p("100"))
            .unwrap()
            .is_none()
    );
    assert!(
        k.observe(PairSymbol::Laggard, 1_000_000, p("50"))
            .unwrap()
            .is_none()
    );
    assert!(
        k.observe(PairSymbol::Leader, 10_000_000, p("101"))
            .unwrap()
            .is_none()
    );
    let s = k
        .observe(PairSymbol::Laggard, 15_000_000, p("50.5"))
        .unwrap()
        .unwrap();
    assert!(s.beta.is_finite());
    assert!(s.posterior_variance > 0.0);
    assert!(s.lag_signal_bps.is_finite());
}

#[test]
fn kalman_rejects_reuse_of_one_leader_return() {
    let mut k = KalmanLeadLagEngine::new(1.0, 1.0, 1e-9, 1e-7).unwrap();
    k.observe(PairSymbol::Leader, 1, p("100")).unwrap();
    k.observe(PairSymbol::Laggard, 1_000_000, p("50")).unwrap();
    k.observe(PairSymbol::Leader, 10_000_000, p("101")).unwrap();
    k.observe(PairSymbol::Laggard, 15_000_000, p("50.5"))
        .unwrap();
    assert!(
        k.observe(PairSymbol::Laggard, 20_000_000, p("50.6"))
            .unwrap()
            .is_none()
    );
}

#[test]
fn kalman_rejects_event_time_reversal_and_long_intervals() {
    let mut k = KalmanLeadLagEngine::new(1.0, 1.0, 1e-9, 1e-7).unwrap();
    k.observe(PairSymbol::Leader, 1, p("100")).unwrap();
    assert_eq!(
        k.observe(PairSymbol::Laggard, 0, p("50")),
        Err(SignalError::NonMonotonicTime)
    );
    k.observe(PairSymbol::Laggard, 1_000, p("50")).unwrap();
    k.observe(PairSymbol::Leader, 100_000_000, p("101"))
        .unwrap();
    assert!(
        k.observe(PairSymbol::Laggard, 101_000_000, p("50.5"))
            .unwrap()
            .is_none()
    );
}

#[test]
fn all_six_engines_are_constructible_with_no_network() {
    let _ = MlOfiEngine::new();
    let _ = MicroPriceEngine::new();
    let _ = VpinToxicityEngine::new(FixedUsd(SCALE)).unwrap();
    let _ = HawkesCascadeEngine::new(1.0).unwrap();
    let _ = AbsorptionEngine::new();
    let _ = KalmanLeadLagEngine::new(1.0, 1.0, 1e-6, 1e-6).unwrap();
}
