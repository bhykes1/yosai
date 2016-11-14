"""
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
"""
import logging
from uuid import uuid4
import time
from yosai.core import (
    AccountException,
    ConsumedTOTPToken,
    IncorrectCredentialsException,
    IndexedPermissionVerifier,
    LockedAccountException,
    SimpleIdentifierCollection,
    SimpleRoleVerifier,
    TOTPToken,
    UsernamePasswordToken,
    authc_abcs,
    authz_abcs,
    cache_abcs,
    realm_abcs,
)

logger = logging.getLogger(__name__)


class AccountStoreRealm(realm_abcs.TOTPAuthenticatingRealm,
                        realm_abcs.AuthorizingRealm,
                        realm_abcs.LockingRealm):
    """
    A Realm interprets information from a datastore.

    Differences between yosai.core.and shiro include:
        1) yosai.core.uses two AccountStoreRealm interfaces to specify authentication
           and authorization
        2) yosai.core.includes support for authorization within the AccountStoreRealm
            - as of shiro v2 alpha rev1693638, shiro doesn't (yet)
    """

    def __init__(self,
                 name='AccountStoreRealm_' + str(uuid4()),
                 account_store=None,
                 authc_verifiers=None,
                 permission_verifier=None,
                 role_verifier=None):
        """
        :authc_verifiers: tuple of Verifier objects
        """
        self.name = name
        self.account_store = account_store
        self.authc_verifiers = authc_verifiers
        self.permission_verifier = permission_verifier
        self.role_verifier = role_verifier

        self.cache_handler = None
        self.token_resolver = self.init_token_resolution()

    @property
    def supported_authc_tokens(self):
        """
        :rtype: list
        :returns: a list of authentication token classes supported by the realm
        """
        return self.token_resolver.keys()

    def supports(self, token):
        return token.__class__ in self.token_resolver

    def init_token_resolution(self):
        token_resolver = {}
        for verifier in self.authc_verifiers:
            for token_cls in verifier.supported_tokens:
                token_resolver[token_cls] = verifier
        return token_resolver

    def do_clear_cache(self, identifier):
        """
        :param identifier: the identifier of a specific source, extracted from
                           the SimpleIdentifierCollection (identifiers)
        """
        msg = "Clearing cache for: " + str(identifier)
        logger.debug(msg)

        self.clear_cached_authc_info(identifier)
        self.clear_cached_authorization_info(identifier)

    def clear_cached_authc_info(self, identifier):
        """
        When cached credentials are no longer needed, they can be manually
        cleared with this method.  However, account credentials may be
        cached with a short expiration time (TTL), making the manual clearing
        of cached credentials an alternative use case.

        :param identifier: the identifier of a specific source, extracted from
                           the SimpleIdentifierCollection (identifiers)
        """
        msg = "Clearing cached authc_info for [{0}]".format(identifier)
        logger.debug(msg)

        self.cache_handler.delete('authentication:' + self.name, identifier)

    def clear_cached_authorization_info(self, identifier):
        """
        This process prevents stale authorization data from being used.
        If any authorization data for an account is changed at runtime, such as
        adding or removing roles and/or permissions, the subclass implementation
        of AccountStoreRealm should clear the cached AuthorizationInfo for that
        account through this method. This ensures that the next call to
        get_authorization_info(PrincipalCollection) will acquire the account's
        fresh authorization data, which is cached for efficient re-use.

        :param identifier: the identifier of a specific source, extracted from
                           the SimpleIdentifierCollection (identifiers)
        """
        msg = "Clearing cached authz_info for [{0}]".format(identifier)
        logger.debug(msg)
        self.cache_handler.delete('authorization:' + self.name, identifier)

    def lock_account(self, identifier):
        """
        :type account: Account
        """
        locked_time = int(time.time() * 1000)  # milliseconds
        self.account_store.lock_account(identifier, locked_time)

    def unlock_account(self, identifier):
        """
        :type account: Account
        """
        self.account_store.unlock_account(identifier)

    # --------------------------------------------------------------------------
    # Authentication
    # --------------------------------------------------------------------------

    def get_authentication_info(self, identifier):
        """
        The default authentication caching policy is to cache an account's
        credentials that are queried from an account store, for a specific
        user, so to facilitate any subsequent authentication attempts for
        that user. Naturally, in order to cache one must have a CacheHandler.
        If a user were to fail to authenticate, perhaps due to an
        incorrectly entered password, during the the next authentication
        attempt (of that user id) the cached account will be readily
        available from cache and used to match credentials, boosting
        performance.

        :returns: an Account object
        """
        account_info = None
        ch = self.cache_handler

        def query_authc_info(self):
            msg = ("Could not obtain cached credentials for [{0}].  "
                   "Will try to acquire credentials from account store."
                   .format(identifier))
            logger.debug(msg)

            # account_info is a dict
            account_info = self.account_store.get_authc_info(identifier)

            if account_info is None:
                msg = "Could not get stored credentials for {0}".format(identifier)
                raise ValueError(msg)

            return account_info

        try:
            msg2 = ("Attempting to get cached credentials for [{0}]"
                    .format(identifier))
            logger.debug(msg2)

            # account_info is a dict
            account_info = ch.get_or_create(domain='authentication:' + self.name,
                                            identifier=identifier,
                                            creator_func=query_authc_info,
                                            creator=self)

        except AttributeError:
            # this means the cache_handler isn't configured
            account_info = query_authc_info(self)
        except ValueError:
            msg3 = ("No account credentials found for identifiers [{0}].  "
                    "Returning None.".format(identifier))
            logger.warning(msg3)

        if account_info:
            account_info['account_id'] = SimpleIdentifierCollection(source_name=self.name,
                                                                    identifier=identifier)
        return account_info

    def authenticate_account(self, authc_token):
        """
        :type authc_token: authc_abcs.AuthenticationToken
        :rtype: dict
        :raises IncorrectCredentialsException:  when authentication fails
        """
        try:
            identifier = authc_token.identifier
        except AttributeError:
            msg = 'Failed to obtain authc_token.identifiers'
            raise AttributeError(msg)

        tc = authc_token.__class__
        try:
            verifier = self.token_resolver[tc]
        except KeyError:
            raise TypeError('realm does not support token type: ', tc.__name__)

        account = self.get_authentication_info(identifier)

        try:
            if account.get('account_locked'):
                msg = "Account Locked:  {0} locked at: {1}".\
                    format(account['account_id'], account['account_locked'])
                raise LockedAccountException(msg)
        except (AttributeError, TypeError):
            if not account:
                msg = "Could not obtain account credentials for: " + str(identifier)
                raise AccountException(msg)

        self.assert_credentials_match(verifier, authc_token, account)

        return account

    def update_failed_attempt(self, authc_token, account):
        cred_type = authc_token.token_info['cred_type']

        attempts = account['authc_info'][cred_type].get('failed_attempts', [])
        attempts.append(int(time.time() * 1000))
        account['authc_info'][cred_type]['failed_attempts'] = attempts

        self.cache_handler.set(domain='authentication:' + self.name,
                               identifier=authc_token.identifier,
                               value=account)
        return account

    def assert_credentials_match(self, verifier, authc_token, account):
        """
        :type verifier: authc_abcs.CredentialsVerifier
        :type authc_token: authc_abcs.AuthenticationToken
        :type account:  account_abcs.Account
        :returns: account_abcs.Account
        :raises IncorrectCredentialsException:  when authentication fails,
                                                including unix epoch timestamps
                                                of recently failed attempts
        """
        cred_type = authc_token.token_info['cred_type']

        try:
            verifier.verify_credentials(authc_token, account['authc_info'])
        except IncorrectCredentialsException:
            updated_account = self.update_failed_attempt(authc_token, account)

            failed_attempts = updated_account['authc_info'][cred_type].\
                get('failed_attempts', [])

            raise IncorrectCredentialsException(failed_attempts)
        except ConsumedTOTPToken:
            account['authc_info'][cred_type]['consumed_token'] = authc_token.credentials
            self.cache_handler.set(domain='authentication:' + self.name,
                                   identifier=authc_token.identifier,
                                   value=account)

    def generate_totp_token(self, account):
        try:
            stored_totp_key = account['authc_info']['totp_key']['credential']
        except KeyError:
            identifier = account['account_id'].primary_identifier
            account = self.get_authentication_info(identifier)
            stored_totp_key = account['authc_info']['totp_key']['credential']

        verifier = self.token_resolver[TOTPToken]
        return verifier.generate_totp_token(stored_totp_key)

    # --------------------------------------------------------------------------
    # Authorization
    # --------------------------------------------------------------------------

    def get_authorization_info(self, identifiers):
        """
        The default caching policy is to cache an account's authorization info,
        obtained from an account store so to facilitate subsequent authorization
        checks. In order to cache, a realm must have a CacheHandler.

        :type identifiers:  subject_abcs.IdentifierCollection

        :returns: Account
        """
        account_info = None
        ch = self.cache_handler

        identifier = identifiers.primary_identifier  # TBD

        def query_authz_info(self):
            msg = ("Could not obtain cached authz_info for [{0}].  "
                   "Will try to acquire authz_info from account store."
                   .format(identifier))
            logger.debug(msg)

            account_info = self.account_store.get_authz_info(identifier)
            if account_info is None:
                msg = "Could not get authz_info for {0}".format(identifier)
                raise ValueError(msg)
            return account_info

        try:
            msg2 = ("Attempting to get cached authz_info for [{0}]"
                    .format(identifier))
            logger.debug(msg2)

            account_info = ch.get_or_create(domain='authorization:' + self.name,
                                            identifier=identifier,
                                            creator_func=query_authz_info,
                                            creator=self)
        except AttributeError:
            # this means the cache_handler isn't configured
            account_info = query_authz_info(self)

        except ValueError:
            msg3 = ("No account authz_info found for identifier [{0}].  "
                    "Returning None.".format(identifier))
            logger.warning(msg3)

        if account_info:
            account_info['account_id'] = SimpleIdentifierCollection(source_name=self.name,
                                                                    identifier=identifier)
        return account_info

    def is_permitted(self, identifiers, permission_s):
        """
        If the authorization info cannot be obtained from the accountstore,
        permission check tuple yields False.

        :type identifiers:  subject_abcs.IdentifierCollection

        :param permission_s: a collection of one or more permissions, represented
                             as string-based permissions or Permission objects
                             and NEVER comingled types
        :type permission_s: list of string(s)

        :yields: tuple(Permission, Boolean)
        """

        account = self.get_authorization_info(identifiers)

        if account is None:
            msg = 'is_permitted:  authz_info returned None for [{0}]'.\
                format(identifiers)
            logger.warning(msg)

            for permission in permission_s:
                yield (permission, False)
        else:
            yield from self.permission_verifier.is_permitted(account['authz_info'],
                                                             permission_s)

    def has_role(self, identifiers, role_s):
        """
        Confirms whether a subject is a member of one or more roles.

        If the authorization info cannot be obtained from the accountstore,
        role check tuple yields False.

        :type identifiers:  subject_abcs.IdentifierCollection

        :param role_s: a collection of 1..N Role identifiers
        :type role_s: Set of String(s)

        :yields: tuple(role, Boolean)
        """
        account = self.get_authorization_info(identifiers)

        if account is None:
            msg = 'has_role:  authz_info returned None for [{0}]'.\
                format(identifiers)
            logger.warning(msg)
            for role in role_s:
                yield (role, False)
        else:
            yield from self.role_verifier.has_role(account['authz_info'], role_s)
