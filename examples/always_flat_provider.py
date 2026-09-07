"""Educational provider that deterministically requests no market exposure.

This example illustrates the narrow signal-provider contract. It has no credentials, network,
database, or broker capability and makes no claim about investment performance.
"""

from __future__ import annotations

from decimal import Decimal

from adaptive_trader.platform.signals import (
    DecisionContext,
    SignalAction,
    SignalEnvelope,
    SignalSourceMode,
)


class EducationalAlwaysFlatProvider:
    """Return an all-flat proposal using only the supplied immutable context."""

    provider_id = "educational_always_flat"
    provider_version = "1"

    def signal_for(self, context: DecisionContext) -> SignalEnvelope:
        """Build a deterministic, non-promotable envelope for the active symbols."""

        symbol_count = len(context.active_symbols)
        return SignalEnvelope.create(
            context=context,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            provider_source_mode=SignalSourceMode.REGISTERED_PLUGIN,
            created_at=context.slot.ready_at,
            availability_mask=(True,) * symbol_count,
            actions=(SignalAction.FLAT,) * symbol_count,
            expected_edge_bps=(None,) * symbol_count,
            proposed_signed_target_inputs=(Decimal(0),) * symbol_count,
            promotable=False,
            paper_submission_eligible=False,
        )
