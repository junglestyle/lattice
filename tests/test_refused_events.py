"""What happens to an event Idea Machine refuses. Against a real Idea Machine database."""

import os

import pytest

from lattice import data

pytestmark = pytest.mark.skipif("LATTICE_DATABASE_URL" not in os.environ, reason="needs LATTICE_DATABASE_URL")


def test_only_an_unknown_kind_is_worth_retrying(monkeypatch):
    """A kind Idea Machine hasn't deployed is NotYet (503, stays queued); any other check it fails is a bad event
    (400, dropped). Provoked by letting Lattice send what its own checks would normally stop."""
    with data.connect() as conn:
        a, b = [str(i) for (i,) in conn.execute("SELECT idea_id FROM pub.ideas ORDER BY idea_id LIMIT 2")]
        monkeypatch.setitem(data.EVENTS, "not_deployed_yet", "idea_id")
        with pytest.raises(data.NotYet):
            data.record(conn, {"kind": "not_deployed_yet", "idea_id": a})
        monkeypatch.setattr(data, "WINNERS", ("first", "second", "tie", "both"))
        with pytest.raises(ValueError, match="feedback_events_compare"):
            data.record(conn, {"kind": "idea_compare", "idea_id": a, "other_idea_id": b, "winner": "both"})
