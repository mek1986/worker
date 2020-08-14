import logging
from contextlib import nullcontext

from shared.torngit.exceptions import TorngitClientError, TorngitError
from shared.utils.sessions import SessionType
from shared.analytics_tracking import track_event

from helpers.match import match
from helpers.environment import is_enterprise
from services.notification.notifiers.base import (
    AbstractBaseNotifier,
    Comparison,
    NotificationResult,
)
from services.repository import get_repo_provider_service
from services.urls import get_commit_url, get_compare_url
from services.yaml.reader import get_paths_from_flags
from services.yaml import read_yaml_field
from typing import Dict


log = logging.getLogger(__name__)


class StatusNotifier(AbstractBaseNotifier):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._repository_service = None

    def is_enabled(self) -> bool:
        return True

    def store_results(self, comparison: Comparison, result: NotificationResult) -> bool:
        pass

    @property
    def name(self):
        return f"status-{self.context}"

    async def build_payload(self, comparison) -> Dict[str, str]:
        raise NotImplementedError()

    def get_upgrade_message(self) -> str:
        # TODO: this is the message in the PR author billing spec but maybe we should add the actual username?
        return "Please activate this user to display a detailed status check"

    def can_we_set_this_status(self, comparison) -> bool:
        head = comparison.head.commit
        pull = comparison.pull
        if (
            self.notifier_yaml_settings.get("only_pulls")
            or self.notifier_yaml_settings.get("base") == "pr"
        ) and not pull:
            return False
        if not match(self.notifier_yaml_settings.get("branches"), head.branch):
            return False
        return True

    def determine_status_check_behavior_to_apply(
        self, comparison, field_name
    ) -> str or None:
        """
        Used for fields that can be set at the global level for all checks, or at the component level for an individual check.
        """
        # Use component level setting, if one is specified
        component_behavior = self.notifier_yaml_settings.get(field_name)
        if component_behavior is not None:
            # Logging so we can track how many users have set "carryforward_behavior"
            # TODO: remove this logging after we decide whether to deprecate that field or not
            if field_name == "carryforward_behavior":
                log.info(
                    "The YAML file explicitly set a value for carryforward_behavior",
                    extra=dict(
                        location="component",
                        field_name=field_name,
                        behavior=component_behavior,
                        commit=comparison.head.commit.commitid,
                        repoid=comparison.head.commit.repoid,
                        notifier_name=self.name,
                        notifier_yaml_settings=self.notifier_yaml_settings,
                    ),
                )

            log.info(
                "Status check specifies a behavior at the component level",
                extra=dict(
                    field_name=field_name,
                    behavior=component_behavior,
                    commit=comparison.head.commit.commitid,
                    repoid=comparison.head.commit.repoid,
                    notifier_name=self.name,
                    notifier_yaml_settings=self.notifier_yaml_settings,
                ),
            )
            return component_behavior

        # Get the value set at the global level via the default_rules key. This can be 'None' if no value was provided.
        # If provided, this is populated either by the YAML file directly or by the defaults set in 'shared'.
        default_rules_behavior = read_yaml_field(
            self.current_yaml, ("coverage", "status", "default_rules", field_name),
        )
        log.info(
            "Using behavior setting from default_rules for status check",
            extra=dict(
                field_name=field_name,
                behavior=default_rules_behavior,
                commit=comparison.head.commit.commitid,
                repoid=comparison.head.commit.repoid,
                notifier_name=self.name,
                notifier_yaml_settings=self.notifier_yaml_settings,
            ),
        )

        # Logging so we can track how many users have set "carryforward_behavior"
        # TODO: remove this logging after we decide whether to deprecate that field or not
        if default_rules_behavior is not None and field_name == "carryforward_behavior":
            log.info(
                "The YAML file explicitly set a value for carryforward_behavior",
                extra=dict(
                    location="default_rules",
                    field_name=field_name,
                    behavior=default_rules_behavior,
                    commit=comparison.head.commit.commitid,
                    repoid=comparison.head.commit.repoid,
                    notifier_name=self.name,
                    notifier_yaml_settings=self.notifier_yaml_settings,
                ),
            )

        return default_rules_behavior

    def flag_coverage_was_uploaded(self, comparison) -> bool:
        """
        Indicates whether coverage was uploaded for any of the flags on this status check.
        If there are no flags on the status check, this will return true.
        If there are multiple flags on the status check, this will return true if at least one of them has uploaded coverage.
        """

        flags_included_in_status_check = set(
            self.notifier_yaml_settings.get("flags") or []
        )

        # initialize to true if there are no flags defined on the check, otherwise false
        flag_coverage_was_uploaded = not bool(
            flags_included_in_status_check
        )  # fyi: calling "bool" on a set returns false if the set is empty

        if comparison.head.report.sessions and bool(
            flags_included_in_status_check
        ):  # only loop if there are flags defined on the check
            for session_id, session in comparison.head.report.sessions.items():
                if session.session_type == SessionType.uploaded:
                    # Figure out which flags in this session are included in this status check
                    status_flags_in_uploaded_session = set(
                        getattr(session, "flags", []) or []
                    ).intersection(flags_included_in_status_check)

                    # if there are any, that means some flag coverage was uploaded for this check, so our work here is done
                    if bool(status_flags_in_uploaded_session):
                        log.debug(
                            "Found flag with uploaded coverage on this check",
                            extra=dict(
                                commit=comparison.head.commit.commitid,
                                repoid=comparison.head.commit.repoid,
                                notifier_name=self.name,
                                flags_included_in_status_check=list(
                                    flags_included_in_status_check
                                ),
                                status_flags_in_uploaded_session=status_flags_in_uploaded_session,
                            ),
                        )

                        flag_coverage_was_uploaded = True
                        break

        log.info(
            "Determined whether flag coverage on this status check was uploaded",
            extra=dict(
                flag_coverage_was_uploaded=flag_coverage_was_uploaded,
                commit=comparison.head.commit.commitid,
                repoid=comparison.head.commit.repoid,
                notifier_name=self.name,
                flags_included_in_status_check=list(flags_included_in_status_check),
            ),
        )
        return flag_coverage_was_uploaded

    def flag_coverage_was_carriedforward(self, comparison) -> bool:
        """
        Indicates whether coverage was carried forward for all the flags on this status check.
        If there are no flags on the status check, this will return false.
        """
        flags_included_in_status_check = set(
            self.notifier_yaml_settings.get("flags") or []
        )
        flags_with_coverage_carriedforward = set()

        if (
            flags_included_in_status_check
            and comparison.head.report.sessions
            and bool(
                flags_included_in_status_check
            )  # only loop if there are flags defined on the check
        ):
            for session_id, session in comparison.head.report.sessions.items():
                if session.session_type == SessionType.carriedforward:
                    # Figure out which flags in this session are included in this status check
                    status_flags_in_carriedforward_session = set(
                        getattr(session, "flags", []) or []
                    ).intersection(flags_included_in_status_check)

                    flags_with_coverage_carriedforward.update(
                        status_flags_in_carriedforward_session
                    )

        # If the sets are equal and not empty, then the status check had flags and all those flags carried forward coverage
        flag_coverage_was_carriedforward = bool(flags_included_in_status_check) and (
            flags_included_in_status_check == flags_with_coverage_carriedforward
        )

        log.info(
            "Determined whether flag coverage on this status check was carried forward",
            extra=dict(
                flag_coverage_was_carriedforward=flag_coverage_was_carriedforward,
                commit=comparison.head.commit.commitid,
                repoid=comparison.head.commit.repoid,
                notifier_name=self.name,
                flags_included_in_status_check=list(flags_included_in_status_check),
                flags_with_coverage_carriedforward=list(
                    flags_with_coverage_carriedforward
                ),
            ),
        )
        return flag_coverage_was_carriedforward

    async def get_diff(self, comparison: Comparison):
        return await comparison.get_diff()

    @property
    def repository_service(self):
        if not self._repository_service:
            self._repository_service = get_repo_provider_service(self.repository)
        return self._repository_service

    def get_notifier_filters(self) -> dict:
        flag_list = self.notifier_yaml_settings.get("flags") or []
        return dict(
            paths=set(
                get_paths_from_flags(self.current_yaml, flag_list)
                + (self.notifier_yaml_settings.get("paths") or [])
            ),
            flags=flag_list,
        )

    async def notify(self, comparison: Comparison):
        payload = None
        if not self.can_we_set_this_status(comparison):
            return NotificationResult(
                notification_attempted=False,
                notification_successful=None,
                explanation="not_fit_criteria",
                data_sent=None,
            )
        # Filter the coverage report based on fields in this notification's YAML settings
        # e.g. if "paths" is specified, exclude the coverage not on those paths
        _filters = self.get_notifier_filters()
        base_full_commit = comparison.base
        try:
            with comparison.head.report.filter(**_filters):
                with (
                    base_full_commit.report.filter(**_filters)
                    if comparison.has_base_report()
                    else nullcontext()
                ):
                    payload = await self.build_payload(comparison)

                    # get the behavior to apply if flag coverage wasn't uploaded
                    flag_coverage_not_uploaded_behavior = self.determine_status_check_behavior_to_apply(
                        comparison, "flag_coverage_not_uploaded_behavior"
                    )

                    if (
                        flag_coverage_not_uploaded_behavior != "include"
                        and not self.flag_coverage_was_uploaded(comparison)
                    ):
                        log.info(
                            "Status check flag coverage was not uploaded, applying behavior based on YAML settings",
                            extra=dict(
                                commit=comparison.head.commit.commitid,
                                repoid=comparison.head.commit.repoid,
                                notifier_name=self.name,
                                flag_coverage_not_uploaded_behavior=flag_coverage_not_uploaded_behavior,
                            ),
                        )

                        if flag_coverage_not_uploaded_behavior == "pass":
                            # Override the payload to pass the status check automatically
                            payload["state"] = "success"
                            payload["message"] = (
                                payload["message"]
                                + " [Auto passed due to carriedforward or missing coverage]"
                            )

                        elif flag_coverage_not_uploaded_behavior == "exclude":
                            # Don't send the notification
                            return NotificationResult(
                                notification_attempted=False,
                                notification_successful=None,
                                explanation="exclude_flag_coverage_not_uploaded_checks",
                                data_sent=None,
                                data_received=None,
                            )

                    # apply carryforward_behavior yaml settings, if specified
                    carryforward_behavior = self.determine_status_check_behavior_to_apply(
                        comparison, "carryforward_behavior"
                    )
                    if (
                        carryforward_behavior is not None
                        and self.flag_coverage_was_carriedforward(comparison)
                    ):
                        log.info(
                            "Status check flag coverage was carried forward, applying carryforward behavior based on YAML settings",
                            extra=dict(
                                commit=comparison.head.commit.commitid,
                                repoid=comparison.head.commit.repoid,
                                notifier_name=self.name,
                                carryforward_behavior=carryforward_behavior,
                            ),
                        )

                        if carryforward_behavior == "pass":
                            # Override the payload to pass the status check automatically
                            payload["state"] = "success"
                            payload["message"] = (
                                payload["message"] + " [Auto passed due to CF Flags]"
                            )

                        elif carryforward_behavior == "include":
                            # Just add a message indicating that the coverage was carried forward
                            payload["message"] = (
                                payload["message"] + " [Carried forward]"
                            )

                        elif carryforward_behavior == "exclude":
                            # Don't send the notification
                            return NotificationResult(
                                notification_attempted=False,
                                notification_successful=None,
                                explanation="exclude_carriedforward_checks",
                                data_sent=None,
                                data_received=None,
                            )

            if (
                comparison.pull
                and self.notifier_yaml_settings.get("base") in ("pr", "auto", None)
                and comparison.base.commit is not None
            ):
                payload["url"] = get_compare_url(
                    comparison.base.commit, comparison.head.commit
                )
            else:
                payload["url"] = get_commit_url(comparison.head.commit)
            return await self.send_notification(comparison, payload)
        except TorngitClientError:
            log.warning(
                "Unable to send status notification to user due to a client-side error",
                exc_info=True,
                extra=dict(
                    repoid=comparison.head.commit.repoid,
                    commit=comparison.head.commit.commitid,
                    notifier_name=self.name,
                ),
            )
            return NotificationResult(
                notification_attempted=True,
                notification_successful=False,
                explanation="client_side_error_provider",
                data_sent=payload,
            )
        except TorngitError:
            log.warning(
                "Unable to send status notification to user due to an unexpected error",
                exc_info=True,
                extra=dict(
                    repoid=comparison.head.commit.repoid,
                    commit=comparison.head.commit.commitid,
                    notifier_name=self.name,
                ),
            )
            return NotificationResult(
                notification_attempted=True,
                notification_successful=False,
                explanation="server_side_error_provider",
                data_sent=payload,
            )

    async def status_already_exists(
        self, comparison, title, state, description
    ) -> bool:
        head = comparison.head.commit
        repository_service = self.repository_service
        statuses = await repository_service.get_commit_statuses(head.commitid)
        if statuses:
            exists = statuses.get(title)
            return (
                exists
                and exists["state"] == state
                and exists["description"] == description
            )
        return False

    def get_status_external_name(self) -> str:
        status_piece = f"/{self.title}" if self.title != "default" else ""
        return f"codecov/{self.context}{status_piece}"

    async def send_notification(self, comparison: Comparison, payload):
        title = self.get_status_external_name()
        repository_service = self.repository_service
        head = comparison.head.commit
        head_report = comparison.head.report
        state = payload["state"]
        message = payload["message"]
        url = payload["url"]
        if not await self.status_already_exists(comparison, title, state, message):
            state = (
                "success" if self.notifier_yaml_settings.get("informational") else state
            )

            # Track state in analytics
            event_name = (
                "Coverage Report Passed"
                if state == "success"
                else "Coverage Report Failed"
            )
            track_event(
                self.repository.ownerid,
                event_name,
                {"state": state, "repository_id": self.repository.repoid},
                is_enterprise(),
            )

            notification_result_data_sent = {
                "title": title,
                "state": state,
                "message": message,
            }
            try:
                res = await repository_service.set_commit_status(
                    commit=head.commitid,
                    status=state,
                    context=title,
                    coverage=float(head_report.totals.coverage),
                    description=message,
                    url=url,
                )
            except TorngitClientError:
                log.warning(
                    "Status not posted because this user can see but not set statuses on this repo",
                    extra=dict(
                        data_sent=notification_result_data_sent,
                        commit=comparison.head.commit.commitid,
                        repoid=comparison.head.commit.repoid,
                    ),
                )
                return NotificationResult(
                    notification_attempted=True,
                    notification_successful=False,
                    explanation="no_write_permission",
                    data_sent=notification_result_data_sent,
                    data_received=None,
                )
            return NotificationResult(
                notification_attempted=True,
                notification_successful=True,
                explanation=None,
                data_sent=notification_result_data_sent,
                data_received={"id": res.get("id", "NO_ID")},
            )
        else:
            log.info(
                "Status already set",
                extra=dict(context=title, description=message, state=state),
            )
            return NotificationResult(
                notification_attempted=False,
                notification_successful=None,
                explanation="already_done",
                data_sent={"title": title, "state": state, "message": message},
            )
