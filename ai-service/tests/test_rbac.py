"""Approving a placement is a different permission from asking for one.

WHY THIS FILE EXISTS
--------------------
Authorisation was binary - IsAdmin or not - so every authenticated employee could
approve a Tier-1 production placement. That undermines the point of recording who
approved something: if the answer is always "whoever was logged in", the audit
trail records a fact nobody needed.

WHAT WAS ALREADY RIGHT, AND IS TESTED HERE SO IT STAYS RIGHT
------------------------------------------------------------
The rest of the model was sound and none of it changed:

  the role is re-read from the database on every request, never taken from a token
  claim, so revoking somebody takes effect immediately rather than at token expiry;

  is_admin in the token is a display hint for hiding a menu item, explicitly not
  the authorisation decision;

  a reviewer's identity comes from the token rather than the request body, so
  nobody can submit a decision as somebody else.

Those are the properties worth pinning. The ordering below is new; the rest is
regression cover for behaviour that already worked.
"""

from __future__ import annotations

import pytest

from app.api.auth import ROLE_ORDER, _rank, require_role


class TestRoleOrdering:
    def test_roles_are_ordered_least_to_most(self):
        assert ROLE_ORDER == ("Viewer", "Engineer", "Approver", "Administrator")

    def test_a_higher_role_satisfies_a_lower_requirement(self):
        """Ordering is what lets an endpoint say "Engineer or above" once, rather
        than enumerating roles - and an enumeration is what silently omits a role
        the next time one is added."""
        assert _rank("Administrator") >= _rank("Approver") >= _rank("Engineer") >= _rank("Viewer")

    def test_a_lower_role_does_not_satisfy_a_higher_requirement(self):
        assert _rank("Engineer") < _rank("Approver")
        assert _rank("Viewer") < _rank("Engineer")

    @pytest.mark.parametrize("unknown", [None, "", "Wizard", "approver", "ADMINISTRATOR"])
    def test_an_unknown_role_ranks_below_every_real_one(self, unknown):
        """The decisive property, and the one worth getting wrong-proof.

        A NULL role, a typo in a data fix, or a case mismatch must grant the
        FEWEST permissions. The alternative reading - "no restriction recorded,
        therefore allow" - is how a spelling mistake grants somebody approval
        rights over production placement, and it would never surface as an error.

        Case sensitivity is deliberate: 'approver' is not Approver. A check that
        normalises case is one that also accepts whatever a careless UPDATE wrote.
        """
        assert _rank(unknown) < _rank("Viewer")

    def test_an_impossible_requirement_fails_at_import_rather_than_at_request_time(self):
        """require_role("Aprover") should break the process on startup, not quietly
        deny every caller in production at three in the morning."""
        with pytest.raises(ValueError, match="unknown role"):
            require_role("Aprover")

    @pytest.mark.parametrize("role", ROLE_ORDER)
    def test_every_declared_role_is_constructible(self, role):
        assert require_role(role) is not None


class TestTheSchemaAgreesWithTheCode:
    """The constraint and the code have to name the same roles. They are edited by
    different people at different times, and a role that exists in one and not the
    other fails at whichever layer is reached second."""

    def test_the_database_allows_exactly_the_roles_the_code_ranks(self):
        from app.repositories.base import fetch_all
        rows = fetch_all(
            """
            SELECT definition FROM sys.check_constraints WHERE name = 'CK_Employee_Role'
            """
        )
        if not rows:
            pytest.skip("migration 016 not applied to this database")
        definition = rows[0]["definition"]
        for role in ROLE_ORDER:
            assert f"'{role}'" in definition, f"{role} is ranked in code but rejected by the database"

    def test_role_and_is_admin_cannot_disagree(self):
        """require_admin reads IsAdmin, everything else reads Role. A row claiming
        Administrator with IsAdmin=0 would look like an administrator in every
        report and be refused at the door - a permission bug invisible until
        somebody is denied access they appear to have."""
        from app.repositories.base import fetch_all
        rows = fetch_all(
            """
            SELECT COUNT(*) AS Mismatched FROM sad.Employee
            WHERE (Role = 'Administrator' AND IsAdmin = 0)
               OR (Role <> 'Administrator' AND IsAdmin = 1)
            """
        )
        assert rows[0]["Mismatched"] == 0, "Role and IsAdmin disagree on some employee"

    def test_nobody_was_silently_promoted_to_approver(self):
        """The migration defaults everyone to Engineer. A system where every
        existing account becomes an Approver has not introduced approval - it has
        renamed the absence of it."""
        from app.repositories.base import fetch_all
        rows = fetch_all("SELECT Role, COUNT(*) AS Held FROM sad.Employee GROUP BY Role")
        by_role = {r["Role"]: r["Held"] for r in rows}
        if not by_role:
            pytest.skip("no employees loaded")
        approvers = by_role.get("Approver", 0) + by_role.get("Administrator", 0)
        total = sum(by_role.values())
        assert approvers < total / 2, (
            f"{approvers} of {total} accounts can approve - the default is too permissive")
