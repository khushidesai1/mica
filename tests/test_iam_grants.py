"""The access identity only adds configured user grants with optimistic policy writes."""

import copy
from unittest.mock import Mock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from mica.agents.analysis import iam_grants as iam
from mica.agents.analysis.config import Settings


@pytest.fixture
def api(monkeypatch):
    client = Mock()
    policy = {
        "version": 3,
        "etag": "v1",
        "auditConfigs": [{"service": "allServices"}],
        "bindings": [
            {"role": "roles/editor", "members": ["user:old@example.com"]},
            {
                "role": "roles/iap.tunnelResourceAccessor",
                "members": ["user:new@example.com"],
                "condition": {"title": "limited", "expression": "false"},
            },
            {"role": "roles/owner", "members": ["group:owners@example.com"]},
        ],
    }
    client.projects().getIamPolicy().execute.side_effect = lambda: copy.deepcopy(policy)
    client.policy = policy
    monkeypatch.setattr(iam, "client", lambda settings: client)
    client.reset_mock()
    return client


def grant():
    iam.grant(
        "test-project",
        "new@example.com",
        ("roles/editor", "roles/iap.tunnelResourceAccessor"),
        settings=Settings(_env_file=None),
    )


def error(status, message):
    return HttpError(
        httplib2.Response({"status": status}), ('{"error":{"message":"' + message + '"}}').encode()
    )


def test_add_only_preserves_members_conditions_audit_and_etag(api):
    before = copy.deepcopy(api.policy)
    grant()
    requested = api.projects().getIamPolicy.call_args.kwargs
    assert requested == {
        "resource": "test-project",
        "body": {"options": {"requestedPolicyVersion": 3}},
    }
    proposed = api.projects().setIamPolicy.call_args.kwargs["body"]["policy"]
    expected = copy.deepcopy(before)
    expected["bindings"][0]["members"].append("user:new@example.com")
    expected["bindings"].append(
        {"role": "roles/iap.tunnelResourceAccessor", "members": ["user:new@example.com"]}
    )
    assert proposed == expected
    assert api.policy == before


def test_existing_unconditional_roles_make_grant_noop(api):
    api.policy["bindings"][0]["members"].append("user:new@example.com")
    del api.policy["bindings"][1]["condition"]
    assert not iam.missing_roles(
        "test-project", "new@example.com", ("roles/editor",), settings=Settings(_env_file=None)
    )
    grant()
    api.projects().setIamPolicy.assert_not_called()


def test_conflicts_reread_fresh_etag_and_are_bounded(api):
    v2 = {**copy.deepcopy(api.policy), "etag": "v2"}
    api.projects().getIamPolicy().execute.side_effect = [copy.deepcopy(api.policy), v2]
    api.projects().setIamPolicy().execute.side_effect = [error(409, "conflict"), {}]
    grant()
    assert api.projects().setIamPolicy.call_args.kwargs["body"]["policy"]["etag"] == "v2"
    api.projects().getIamPolicy().execute.side_effect = lambda: copy.deepcopy(api.policy)
    api.projects().setIamPolicy().execute.side_effect = error(409, "conflict")
    api.reset_mock()
    with pytest.raises(iam.GrantFailed, match="changed repeatedly"):
        grant()
    assert api.projects().setIamPolicy.call_count == 3


@pytest.mark.parametrize(
    "status,message,expected",
    [
        (400, "User does not exist", "No Google account"),
        (400, "constraints/iam.allowedPolicyMemberDomains", "sharing policy"),
        (403, "Permission denied", "lacks permission"),
    ],
)
def test_definite_refusals_are_explained_without_provider_details(api, status, message, expected):
    api.projects().setIamPolicy().execute.side_effect = error(status, message)
    with pytest.raises(iam.GrantFailed, match=expected):
        grant()


@pytest.mark.parametrize(
    "email,roles",
    [
        ("group:team@example.com", ("roles/editor",)),
        ("serviceAccount:bot@example.com", ("roles/editor",)),
        ("a@example.com,b@example.com", ("roles/editor",)),
        ("user@example.com", ("roles/owner",)),
        ("user@example.com", ("roles/iam.securityAdmin",)),
        ("user@example.com", ()),
    ],
)
def test_bad_grantees_and_roles_never_touch_api(api, email, roles):
    with pytest.raises(ValueError):
        iam.grant("test-project", email, roles, settings=Settings(_env_file=None))
    api.projects.assert_not_called()


def test_missing_etag_never_writes(api):
    api.policy.pop("etag")
    with pytest.raises(iam.GrantFailed, match="no policy version"):
        grant()
    api.projects().setIamPolicy.assert_not_called()


def test_ambiguous_set_policy_failure_can_be_retried_safely(api):
    api.projects().setIamPolicy().execute.side_effect = error(503, "retry later")
    with pytest.raises(HttpError):
        grant()
    api.policy["bindings"][0]["members"].append("user:new@example.com")
    del api.policy["bindings"][1]["condition"]
    api.reset_mock()
    grant()
    api.projects().setIamPolicy.assert_not_called()
