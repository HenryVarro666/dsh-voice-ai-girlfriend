"""Recompute the old/fixed cancellation report using shared clock-safe metrics."""

from report_xvector_integration import main

if __name__ == "__main__":
    main(
        ("baseline", "fixed"),
        "apex cancellation lifecycle | ICL both arms | production unchanged",
    )
