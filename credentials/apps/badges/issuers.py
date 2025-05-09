"""
This module provides classes for issuing badge credentials to users.
"""

from datetime import datetime

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils.translation import gettext as _

from credentials.apps.badges.accredible.api_client import AccredibleAPIClient
from credentials.apps.badges.accredible.data import (
    AccredibleBadgeData,
    AccredibleCredential,
    AccredibleExpireBadgeData,
    AccredibleExpiredCredential,
    AccredibleRecipient,
)
from credentials.apps.badges.credly.api_client import CredlyAPIClient
from credentials.apps.badges.credly.data import CredlyBadgeData
from credentials.apps.badges.exceptions import BadgeProviderError
from credentials.apps.badges.models import (
    AccredibleBadge,
    AccredibleGroup,
    BadgeTemplate,
    CredlyBadge,
    CredlyBadgeTemplate,
    UserCredential,
)
from credentials.apps.badges.signals.signals import notify_badge_awarded, notify_badge_revoked
from credentials.apps.core.api import get_user_by_username
from credentials.apps.credentials.constants import UserCredentialStatus
from credentials.apps.credentials.issuers import AbstractCredentialIssuer


REVOCATION_STATES = {
    CredlyBadge: CredlyBadge.STATES.revoked,
    AccredibleBadge: AccredibleBadge.STATES.expired,
}


class BadgeTemplateIssuer(AbstractCredentialIssuer):
    """
    Issues BadgeTemplate credentials to users.
    """

    issued_credential_type = BadgeTemplate
    issued_user_credential_type = UserCredential

    def get_credential(self, credential_id):
        """
        Get credential by id.
        """

        return self.issued_credential_type.objects.get(id=credential_id)

    @transaction.atomic
    def issue_credential(
        self,
        credential,
        username,
        status=UserCredentialStatus.AWARDED,
        attributes=None,
        date_override=None,
        request=None,
        lms_user_id=None,
    ):  # pylint: disable=too-many-positional-arguments
        """
        Issue a credential to the user.

        This action is idempotent. If the user has already earned the credential, a new one WILL NOT be issued. The
        existing credential WILL be modified.

        Arguments:
            credential (AbstractCredential): Type of credential to issue.
            username (str): username of user for which credential required
            status (str): status of credential
            attributes (List[dict]): optional list of attributes that should be associated with the issued credential.
            request (HttpRequest): request object to build program record absolute uris

        Returns:
            UserCredential
        """

        user_credential, __ = self.issued_user_credential_type.objects.get_or_create(
            username=username,
            credential_content_type=ContentType.objects.get_for_model(credential),
            credential_id=credential.id,
            defaults={
                "status": status,
            },
        )
        if not user_credential.state == REVOCATION_STATES.get(self.issued_user_credential_type):
            user_credential.status = status
            user_credential.save()

        self.set_credential_attributes(user_credential, attributes)
        self.set_credential_date_override(user_credential, date_override)

        return user_credential

    def award(self, *, username, credential_id):
        """
        Awards a badge.

        Creates user credential record for the given badge template, for a given user.
        Notifies about the awarded badge (public signal).

        Returns: UserCredential
        """

        credential = self.get_credential(credential_id)
        user_credential = self.issue_credential(credential, username)

        notify_badge_awarded(user_credential)
        return user_credential

    def revoke(self, credential_id, username):
        """
        Revokes a badge.

        Changes user credential status to REVOKED, for a given user.
        Notifies about the revoked badge (public signal).

        Returns: UserCredential
        """

        credential = self.get_credential(credential_id)
        user_credential = self.issue_credential(credential, username, status=UserCredentialStatus.REVOKED)

        notify_badge_revoked(user_credential)
        return user_credential


class CredlyBadgeTemplateIssuer(BadgeTemplateIssuer):
    """
    Issues CredlyBadgeTemplate credentials to users.
    """

    issued_credential_type = CredlyBadgeTemplate
    issued_user_credential_type = CredlyBadge

    def issue_credly_badge(self, *, user_credential):
        """
        Requests Credly service for external badge issuing based on internal user credential (CredlyBadge).
        """

        user = get_user_by_username(user_credential.username)
        badge_template = user_credential.credential

        credly_badge_data = CredlyBadgeData(
            recipient_email=user.email,
            issued_to_first_name=(user.first_name or user.username),
            issued_to_last_name=(user.last_name or user.username),
            badge_template_id=str(badge_template.uuid),
            issued_at=badge_template.created.strftime("%Y-%m-%d %H:%M:%S %z"),
        )

        try:
            credly_api = CredlyAPIClient(badge_template.organization.uuid)
            response = credly_api.issue_badge(credly_badge_data)
        except BadgeProviderError:
            user_credential.state = "error"
            user_credential.save()
            raise

        user_credential.external_uuid = response.get("data").get("id")
        user_credential.state = response.get("data").get("state")
        user_credential.save()

    def revoke_credly_badge(self, credential_id, user_credential):
        """
        Requests Credly service for external badge revoking based on internal user credential (CredlyBadge).
        """

        credential = self.get_credential(credential_id)
        credly_api = CredlyAPIClient(credential.organization.uuid)
        revoke_data = {
            "reason": _("Open edX internal user credential was revoked"),
        }
        try:
            response = credly_api.revoke_badge(user_credential.external_uuid, revoke_data)
        except BadgeProviderError:
            user_credential.state = "error"
            user_credential.save()
            raise

        user_credential.state = response.get("data").get("state")
        user_credential.save()

    def award(self, *, username, credential_id):
        """
        Awards a Credly badge.

        - Creates user credential record for the given badge template, for a given user;
        - Notifies about the awarded badge (public signal);
        - Issues external Credly badge (Credly API);

        Returns: (CredlyBadge) user credential
        """

        credly_badge = super().award(username=username, credential_id=credential_id)

        # do not issue new badges if the badge was issued already
        if not credly_badge.propagated:
            self.issue_credly_badge(user_credential=credly_badge)

        return credly_badge

    def revoke(self, credential_id, username):
        """
        Revokes a Credly badge.

        - Changes user credential status to REVOKED, for a given user;
        - Notifies about the revoked badge (public signal);
        - Revokes external Credly badge (Credly API);

        Returns: (CredlyBadge) user credential
        """

        user_credential = super().revoke(credential_id, username)
        if user_credential.propagated:
            self.revoke_credly_badge(credential_id, user_credential)
        return user_credential


class AccredibleBadgeTemplateIssuer(BadgeTemplateIssuer):
    """
    Issues AccredibleGroup credentials to users.
    """

    issued_credential_type = AccredibleGroup
    issued_user_credential_type = AccredibleBadge

    def issue_accredible_badge(self, *, user_credential):
        """
        Requests Accredible service for external badge issuing based on internal user credential (AccredibleBadge).
        """

        user = get_user_by_username(user_credential.username)
        group = user_credential.credential

        accredible_badge_data = AccredibleBadgeData(
            credential=AccredibleCredential(
                recipient=AccredibleRecipient(
                    name=user.get_full_name() or user.username,
                    email=user.email,
                ),
                group_id=group.id,
                name=group.name,
                issued_on=user_credential.created.strftime("%Y-%m-%d %H:%M:%S %z"),
                complete=True,
            )
        )

        try:
            accredible_api = AccredibleAPIClient(group.api_config.id)
            response = accredible_api.issue_badge(accredible_badge_data)
        except BadgeProviderError:
            user_credential.state = "error"
            user_credential.save()
            raise

        user_credential.external_id = response.get("credential").get("id")
        user_credential.state = AccredibleBadge.STATES.accepted
        user_credential.save()

    def revoke_accredible_badge(self, credential_id, user_credential):
        """
        Requests Accredible service for external badge expiring based on internal user credential (AccredibleBadge).
        """

        credential = self.get_credential(credential_id)
        accredible_api_client = AccredibleAPIClient(credential.api_config.id)
        revoke_badge_data = AccredibleExpireBadgeData(
            credential=AccredibleExpiredCredential(expired_on=datetime.now().strftime("%Y-%m-%d %H:%M:%S %z"))
        )

        try:
            accredible_api_client.revoke_badge(user_credential.external_id, revoke_badge_data)
        except BadgeProviderError:
            user_credential.state = "error"
            user_credential.save()
            raise

        user_credential.state = AccredibleBadge.STATES.expired
        user_credential.save()

    def award(self, *, username, credential_id):
        """
        Awards a Accredible badge.

        - Creates user credential record for the group, for a given user;
        - Notifies about the awarded badge (public signal);
        - Issues external Accredible badge (Accredible API);

        Returns: (AccredibleBadge) user credential
        """

        accredible_badge = super().award(username=username, credential_id=credential_id)

        # do not issue new badges if the badge was issued already
        if not accredible_badge.propagated:
            self.issue_accredible_badge(user_credential=accredible_badge)

        return accredible_badge

    def revoke(self, credential_id, username):
        """
        Revokes a Accredible badge.

        - Changes user credential status to REVOKED, for a given user;
        - Notifies about the revoked badge (public signal);
        - Expire external Accredible badge (Accredible API);

        Returns: (AccredibleBadge) user credential
        """

        user_credential = super().revoke(credential_id, username)
        if user_credential.propagated:
            self.revoke_accredible_badge(credential_id, user_credential)
        return user_credential
