"""Interactively bootstrap the first local administrator."""

from __future__ import annotations

import getpass

from django.core.management.base import BaseCommand, CommandError

from agora.persistence.authentication import (
    BootstrapAlreadyComplete,
    PasswordPolicyError,
    bootstrap_first_administrator,
)
from agora.persistence.models import User
from agora.persistence.names import InvalidSoeid


class Command(BaseCommand):
    help = "Create the first administrator through hidden interactive password prompts."

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument(
            "--soeid",
            help="First administrator SOEID; the password is never accepted as an argument.",
        )

    def handle(self, *args, **options) -> None:  # type: ignore[no-untyped-def]
        del args
        if User.objects.exists():
            raise CommandError(
                "First-administrator bootstrap is available only before any users exist."
            )

        soeid = options.get("soeid") or input("First administrator SOEID: ")
        password = getpass.getpass("First administrator password: ")
        confirmation = getpass.getpass("Confirm first administrator password: ")
        if password != confirmation:
            raise CommandError("Passwords do not match.")

        try:
            bootstrap_first_administrator(soeid, password)
        except BootstrapAlreadyComplete as error:
            raise CommandError(
                "First-administrator bootstrap is available only before any users exist."
            ) from error
        except InvalidSoeid as error:
            raise CommandError("The SOEID is not valid.") from error
        except PasswordPolicyError as error:
            raise CommandError("The password does not meet the configured policy.") from error

        self.stdout.write(self.style.SUCCESS("First administrator created."))
