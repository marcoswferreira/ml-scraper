from ml_automation.models.coupon import (
    MercadoLivreCoupon,
    compute_coupon_score,
    filter_relevant_coupons,
)
from ml_automation.models.product import AffiliateLinkResult, AffiliateProduct

__all__ = [
    "AffiliateProduct",
    "AffiliateLinkResult",
    "MercadoLivreCoupon",
    "compute_coupon_score",
    "filter_relevant_coupons",
]
