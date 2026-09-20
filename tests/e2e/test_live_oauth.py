import os

import pytest


@pytest.mark.live
def test_live_oauth_capability_matrix_must_be_explicitly_enabled():
    if os.environ.get("PEER_REVIEWER_LIVE_OAUTH") != "1":
        pytest.skip("live OAuth acceptance is NO-GO until both capability probes are verified")
    pytest.fail("live OAuth harness is intentionally blocked: Claude limit telemetry is still unavailable")
