use crate::types::{RiskContext, RiskReason};
use trading_core_types::{
    ComplianceDecision, EquitiesCompliance, ForexCommoditiesCompliance, OrderIntent,
    VenueComplianceTrait, VenueId,
};

/// Non-crypto venue policies are applied at a mandatory trust boundary.
/// SSR repricing is reported as a REJECTION, never silently modified; the
/// future order router must retry a separately validated tick-aligned order.
/// ETB is not a borrow locate; the caller must supply a true locate proof.
pub(crate) fn venue_entry_policy(order: &OrderIntent, ctx: &RiskContext) -> Result<(), RiskReason> {
    let m = ctx.market;
    match m.venue {
        VenueId::Equities => match EquitiesCompliance.evaluate(order, &ctx.equities) {
            ComplianceDecision::Allow => Ok(()),
            ComplianceDecision::Reprice { .. } => Err(RiskReason::ShortSalePriceRestricted),
            ComplianceDecision::Block(_) if !ctx.equities.located_for_open_short => {
                Err(RiskReason::ShortLocateMissing)
            }
            ComplianceDecision::Block(_) => Err(RiskReason::ComplianceBlackout),
        },
        VenueId::Forex | VenueId::Commodities => {
            if ctx.account.rollover_or_policy_blackout {
                return Err(RiskReason::ComplianceBlackout);
            }
            match ForexCommoditiesCompliance.evaluate(order, &ctx.equities) {
                ComplianceDecision::Allow => Ok(()),
                _ => Err(RiskReason::ComplianceBlackout),
            }
        }
        VenueId::BinanceUsdM | VenueId::BybitLinear => Ok(()),
    }
}
